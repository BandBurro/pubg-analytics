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
