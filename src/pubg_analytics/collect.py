"""Collector CLI.

Lands raw PUBG data on disk, untouched, partitioned by shard and match date.
Nothing here interprets the data — that's the job of the Silver layer. Bronze
is append-only and immutable so the whole warehouse can be rebuilt from it.

    pubg discover           # find new match ids via /samples
    pubg fetch --limit 200  # download match + telemetry for pending ids
    pubg run --limit 200    # discover then fetch (what cron should call)
    pubg status             # ledger summary
"""

import asyncio
import gzip
import os
from pathlib import Path

import httpx
import orjson
import typer

from .api import MatchGone, PubgClient, extract_match_meta, extract_telemetry_url
from .config import settings
from .ledger import Ledger

app = typer.Typer(add_completion=False, help="Collect raw PUBG match telemetry.")


def _partition(kind: str, match_id: str, created_at: str | None) -> Path:
    """data/raw/<kind>/shard=steam/dt=YYYY-MM-DD/<match_id>.json.gz"""
    dt = (created_at or "unknown")[:10]
    return settings.raw_dir / kind / f"shard={settings.shard}" / f"dt={dt}" / f"{match_id}.json.gz"


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write via a temp file so a crash can never leave a half-written blob."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as fh:
        fh.write(payload)
    os.replace(tmp, path)


async def _fetch_one(client: PubgClient, ledger: Ledger, match_id: str) -> str:
    try:
        match = await client.get_match(match_id)
        meta = extract_match_meta(match)
        telemetry_url = extract_telemetry_url(match)
        if not telemetry_url:
            ledger.mark_failed(match_id, "no telemetry asset in match payload")
            return "no-telemetry"

        raw = await client.download_telemetry(telemetry_url)

        match_path = _partition("matches", match_id, meta["created_at"])
        tele_path = _partition("telemetry", match_id, meta["created_at"])
        _write_atomic(match_path, orjson.dumps(match))
        _write_atomic(tele_path, raw)

        # Cheap sanity count; the real parsing happens in Silver.
        try:
            event_count = len(orjson.loads(raw))
        except orjson.JSONDecodeError:
            event_count = -1

        ledger.mark_done(
            match_id,
            telemetry_path=str(tele_path),
            event_count=event_count,
            match_created_at=meta["created_at"],
            map_name=meta["map_name"],
            game_mode=meta["game_mode"],
        )
        return "ok"
    except MatchGone as exc:
        ledger.mark_failed(match_id, str(exc), gone=True)
        return "gone"
    except httpx.TransportError as exc:
        # Connectivity or TLS problem — tells us nothing about this match, so
        # don't spend an attempt on it.
        ledger.mark_retryable(match_id, f"{type(exc).__name__}: {exc}")
        return "network"
    except Exception as exc:  # noqa: BLE001 - collector must survive one bad match
        ledger.mark_failed(match_id, f"{type(exc).__name__}: {exc}")
        return "error"


TLS_HINT = """
Could not establish a trusted HTTPS connection.

If the error mentions certificate verification, something on this network is
intercepting TLS (a corporate firewall or proxy re-signing certificates). Check
with:

    openssl s_client -connect api.pubg.com:443 -servername api.pubg.com \\
      </dev/null 2>/dev/null | openssl x509 -noout -issuer

If the issuer is not a public CA, switch to an uninterrupted network — a
personal hotspot or home wifi. Collection resumes automatically; nothing is lost.
"""


def _explain_transport_error(exc: httpx.TransportError) -> None:
    typer.echo(f"network error: {type(exc).__name__}: {exc}", err=True)
    if "CERTIFICATE_VERIFY_FAILED" in str(exc):
        typer.echo(TLS_HINT, err=True)


async def _discover(since: str | None) -> int:
    key = settings.require_key()
    try:
        async with PubgClient(
            api_key=key,
            base_url=settings.base_url,
            rpm=settings.rpm,
            concurrency=settings.concurrency,
        ) as client:
            ids = await client.sample_match_ids(since)
    except httpx.TransportError as exc:
        _explain_transport_error(exc)
        raise typer.Exit(code=1) from None
    with Ledger(settings.ledger_path) as ledger:
        new = ledger.add_discovered(ids)
    typer.echo(f"sample returned {len(ids)} match ids, {new} new")
    return new


async def _fetch(limit: int) -> None:
    key = settings.require_key()
    with Ledger(settings.ledger_path) as ledger:
        pending = ledger.pending(limit)
        if not pending:
            typer.echo("nothing pending")
            return
        typer.echo(f"fetching {len(pending)} matches...")
        async with PubgClient(
            api_key=key,
            base_url=settings.base_url,
            rpm=settings.rpm,
            concurrency=settings.concurrency,
        ) as client:
            results = await asyncio.gather(
                *(_fetch_one(client, ledger, mid) for mid in pending)
            )
        tally: dict[str, int] = {}
        for r in results:
            tally[r] = tally.get(r, 0) + 1
        typer.echo("  " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))


@app.command()
def discover(
    since: str = typer.Option(None, help="ISO8601 UTC start, e.g. 2026-08-14T00:00:00Z"),
) -> None:
    """Find new match ids from the /samples endpoint."""
    asyncio.run(_discover(since))


@app.command()
def fetch(limit: int = typer.Option(200, help="Max matches to fetch this run.")) -> None:
    """Download match detail and telemetry for pending match ids."""
    asyncio.run(_fetch(limit))


@app.command()
def run(limit: int = typer.Option(200, help="Max matches to fetch this run.")) -> None:
    """Discover then fetch. This is the entry point for a scheduled run."""

    async def _both() -> None:
        await _discover(None)
        await _fetch(limit)

    asyncio.run(_both())


@app.command()
def shred(
    limit: int = typer.Option(0, help="Max matches to shred; 0 means everything pending."),
    batch: int = typer.Option(250, help="Matches per Parquet part file."),
) -> None:
    """Shred raw telemetry JSON into typed Parquet tables."""
    from .shred import Shredder, match_definition, read_gz_json, telemetry_to_manifest_path

    with Ledger(settings.ledger_path) as ledger:
        todo = ledger.unshredded(limit if limit > 0 else 10_000_000)
        if not todo:
            typer.echo("nothing to shred")
            return
        typer.echo(f"shredding {len(todo):,} matches in batches of {batch}...")

        part = ledger.next_shred_part()
        totals: dict[str, int] = {}
        skipped = 0

        for start in range(0, len(todo), batch):
            chunk = todo[start : start + batch]
            sh = Shredder(settings.bronze_dir)
            ok: list[str] = []
            for match_id, tele_path in chunk:
                try:
                    events = read_gz_json(Path(tele_path))
                    manifest = read_gz_json(telemetry_to_manifest_path(tele_path))
                    tele_mid, ping = match_definition(events)
                    sh.add_manifest(manifest, tele_mid, ping)
                    sh.add_telemetry(match_id, events)
                    ok.append(match_id)
                except (OSError, orjson.JSONDecodeError, KeyError) as exc:
                    typer.echo(f"  skip {match_id}: {type(exc).__name__}: {exc}", err=True)
                    skipped += 1

            written = sh.flush(part)
            ledger.mark_shredded(ok, part)
            for k, v in written.items():
                totals[k] = totals.get(k, 0) + v
            typer.echo(
                f"  part {part:05d}: {len(ok)} matches -> "
                + ", ".join(f"{k}={v:,}" for k, v in sorted(written.items()))
            )
            part += 1

    typer.echo("\ntotals:")
    for k, v in sorted(totals.items()):
        typer.echo(f"  {k:<18} {v:>12,}")
    if skipped:
        typer.echo(f"skipped {skipped} match(es)")


async def _cohort(limit: int, min_matches: int) -> None:
    """Expand collection along players rather than random matches."""
    import duckdb

    key = settings.require_key()
    wh = settings.data_dir / "warehouse.duckdb"
    if not wh.exists():
        raise SystemExit(f"no warehouse at {wh} — run `just build` first")

    con = duckdb.connect(str(wh), read_only=True)
    seeds = [
        r[0]
        for r in con.execute(
            """
            select account_id
            from main_gold.dim_player
            where not is_bot and analytical_matches >= ?
            order by analytical_matches desc, career_kills desc
            limit ?
            """,
            [min_matches, limit],
        ).fetchall()
    ]
    con.close()

    if not seeds:
        typer.echo("no seed players found")
        return
    typer.echo(f"expanding {len(seeds):,} seed players in batches of 10...")

    found: dict[str, list[str]] = {}
    try:
        async with PubgClient(
            api_key=key,
            base_url=settings.base_url,
            rpm=settings.rpm,
            concurrency=settings.concurrency,
        ) as client:
            for start in range(0, len(seeds), 10):
                batch = seeds[start : start + 10]
                try:
                    found.update(await client.get_players_matches(batch))
                except MatchGone:
                    # Accounts can vanish (bans, renames). Not fatal.
                    continue
                done = min(start + 10, len(seeds))
                typer.echo(f"  {done}/{len(seeds)} players, {len(found)} resolved")
    except httpx.TransportError as exc:
        _explain_transport_error(exc)
        raise typer.Exit(code=1) from None

    match_ids = sorted({m for ms in found.values() for m in ms})
    with Ledger(settings.ledger_path) as ledger:
        new = ledger.add_discovered(match_ids)
    per = len(match_ids) / len(found) if found else 0
    typer.echo(
        f"{len(found):,} players -> {len(match_ids):,} distinct matches "
        f"({per:.1f}/player), {new:,} new"
    )
    typer.echo("run `just fetch` to download them")


@app.command()
def cohort(
    limit: int = typer.Option(200, help="How many seed players to expand."),
    min_matches: int = typer.Option(2, help="Only seed players with at least this many."),
) -> None:
    """Discover matches by player history instead of random sampling.

    /samples gives breadth: random matches, almost no repeated players, so skill
    ratings never converge. This gives depth — the repeated observations a rating
    system actually needs.
    """
    asyncio.run(_cohort(limit, min_matches))


@app.command()
def rate() -> None:
    """Run the skill-rating engine over the warehouse and emit rating updates."""
    import duckdb

    from .ratings import group_matches, run_ratings, write_updates

    wh = settings.data_dir / "warehouse.duckdb"
    if not wh.exists():
        raise SystemExit(f"no warehouse at {wh} — run `just build` first")

    con = duckdb.connect(str(wh), read_only=True)
    from .ratings import RATING_QUERY

    cols = [d[0] for d in con.execute(RATING_QUERY).description]
    rows = [dict(zip(cols, r, strict=True)) for r in con.execute(RATING_QUERY).fetchall()]
    con.close()
    typer.echo(f"rating over {len(rows):,} player-match rows...")

    groups = group_matches(rows)
    updates = run_ratings(groups)
    if not updates:
        typer.echo("no rateable matches (need 2+ human teams per match)")
        return

    path = write_updates(updates, settings.data_dir / "ratings")
    rated = len({u["account_id"] for u in updates})
    repeat = len(
        {u["account_id"] for u in updates if u["games_played_before"] > 0}
    )
    typer.echo(f"  matches grouped   : {len(groups):,}")
    typer.echo(f"  rating updates    : {len(updates):,}")
    typer.echo(f"  players rated     : {rated:,}")
    typer.echo(f"  with prior history: {repeat:,}")
    typer.echo(f"  wrote {path}")


@app.command()
def study(
    out: str = typer.Option("reports/estimator_study.md", help="Where to write the report."),
) -> None:
    """Run the estimator studies against synthetic ground truth."""
    from . import study as st

    lines: list[str] = ["# Estimator study", ""]
    lines.append(
        "Measured against synthetic data with known truth — the one thing real "
        "data cannot do, because you can only ask *how wrong is this estimator* "
        "when you already know the answer."
    )

    typer.echo("1/5 sample-size floor...")
    rows = st.study_sample_size_floor()
    lines += ["", "## 1. How many fights before a win rate is trustworthy?", "",
              "True rate 0.60, 2,000 replicates per row.", "",
              "| n | mean abs error | p90 abs error | within 2pp | within 5pp |",
              "|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['n']:,} | {r['mean_abs_error']:.4f} | {r['p90_abs_error']:.4f} "
            f"| {r['within_2pp']:.0%} | {r['within_5pp']:.0%} |"
        )

    lines += ["",
              "The `within 2pp` column is non-monotonic at tiny n, and that is a "
              "lattice artefact rather than a property of the estimator: with 10 "
              "fights the only achievable estimates are 0.0, 0.1, ... 1.0, so "
              "\"within 2pp\" really means \"landed exactly on 0.60\". Read the "
              "p90 error column, which is monotonic."]

    typer.echo("2/5 shrinkage crossover...")
    rows2 = st.study_shrinkage_crossover()
    lines += ["", "## 2. Where does shrinkage help, and where does it over-smooth?", "",
              "RMSE against true rate, by prior weight and cell size.", "",
              "| prior weight | stratum | cells | RMSE |", "|---|---|---|---|"]
    for r in rows2:
        lines.append(f"| {r['prior_weight']} | {r['stratum']} | {r['cells']:,} | {r['rmse']:.4f} |")

    typer.echo("3/5 confounded rate...")
    c = st.study_confounded_rate()
    lines += ["", "## 3. Bias from an unmeasured confounder", "",
              "Two weapons with **identical** true lethality; one is used mostly "
              "against weak opponents.", "",
              "| weapon | naive rate | stratified rate |", "|---|---|---|",
              f"| A (85% vs weak) | {c['weapon_a']['naive']:.4f} "
              f"| {c['weapon_a']['adjusted']:.4f} |",
              f"| B (15% vs weak) | {c['weapon_b']['naive']:.4f} "
              f"| {c['weapon_b']['adjusted']:.4f} |",
              "",
              f"Naive gap: **{c['naive_gap_pp']} pp**. After stratifying: "
              f"**{c['adjusted_gap_pp']} pp**. The entire apparent difference was "
              "who each weapon happened to face."]

    typer.echo("4/5 leakage cost...")
    lk = st.study_leakage_cost()
    lines += ["", "## 4. What point-in-time leakage buys that isn't real", "",
              f"- AUC from the rating known **before** the match: **{lk.auc_correct:.4f}**",
              f"- AUC from the rating computed **after** it: **{lk.auc_leaky:.4f}**",
              f"- Inflation: **{lk.inflation:+.4f}** over {lk.observations:,} observations",
              "", lk.notes[0]]

    typer.echo("5/5 cluster design effect...")
    cd = st.study_cluster_design_effect()
    lines += ["", "## 5. Confidence intervals when clustering is ignored", "",
              f"- {cd['phases_per_match']} correlated phases per match, "
              f"{cd['observations_per_replicate']:,} observations",
              f"- Naive standard error: {cd['naive_se']:.5f}",
              f"- Actual standard error: {cd['empirical_se']:.5f}",
              f"- **Understated by {cd['se_understated_by']:.2f}x** "
              f"(design effect {cd['design_effect']:.2f})",
              "",
              f"Intervals come out **{cd['ci_too_narrow_pct']:.0f}% too narrow**, "
              "which is how a null result gets reported as significant."]

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    typer.echo(f"\nwrote {path}")


@app.command()
def status() -> None:
    """Summarise the collection ledger."""
    with Ledger(settings.ledger_path) as ledger:
        s = ledger.stats()
        shred_s = ledger.shred_stats()
    events = s.pop("events", 0)
    total = s.pop("total", 0)
    typer.echo(f"data dir : {settings.data_dir.resolve()}")
    typer.echo(f"shard    : {settings.shard}")
    typer.echo(f"matches  : {total}")
    for status_name, n in sorted(s.items()):
        typer.echo(f"  {status_name:<8} {n}")
    typer.echo(f"events   : {events:,}")
    typer.echo(f"shredded : {shred_s['shredded']:,} of {shred_s['fetched']:,}", nl=False)
    typer.echo(f"  ({shred_s['remaining']:,} pending)" if shred_s["remaining"] else "")


if __name__ == "__main__":
    app()
