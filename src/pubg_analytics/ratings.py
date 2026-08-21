"""Skill rating engine.

Deliberately not SQL. A rating system is a sequential fold: match N's update
depends on the state left by match N-1. Expressing that as recursive CTEs would
be slow, unreadable and untestable. Python owns the fold; dbt consumes the
`fact_rating_update` table it emits.

Three modelling decisions worth stating plainly:

1. **Plackett-Luce over placements.** A battle royale result is a ranking of many
   teams, not a win/loss. Plackett-Luce is built for exactly that and extracts far
   more signal per match than collapsing the result to binary.

2. **Human-relative team ranks.** PUBG pads lobbies with bots. Teams are ranked
   among the *human* teams present, so placing 20th in a bot-heavy lobby doesn't
   read as a stronger result than 20th against humans.

3. **Full recompute, not incremental.** Rating systems are order-dependent, so
   incremental state introduces a class of correctness bugs for no benefit at this
   scale — a fold over 100k matches takes seconds. Revisit if this ever exceeds
   a few million matches.

Every update is emitted with its pre- and post-state, which is what makes
point-in-time-correct features possible downstream: the rating *before* a match
is the only one a model may use to predict it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
from openskill.models import PlackettLuce

MODEL_VERSION = "plackett_luce.v1"

RATING_QUERY = """
select
    match_id,
    match_start_ts,
    team_id,
    account_id,
    human_placement
from main_gold.fact_player_match
where is_analytical
  and not is_bot
  and human_placement is not null
  and team_id is not null
order by match_start_ts, match_id, team_id, account_id
"""


@dataclass
class MatchGroup:
    match_id: str
    match_start_ts: Any
    # (team_id, rank_among_human_teams, [account_id, ...])
    teams: list[tuple[int, int, list[str]]]


def group_matches(rows: list[dict]) -> list[MatchGroup]:
    """Fold flat player rows into per-match team structures, ranked."""
    by_match: dict[str, dict] = {}
    for r in rows:
        m = by_match.setdefault(r["match_id"], {"ts": r["match_start_ts"], "teams": {}})
        t = m["teams"].setdefault(r["team_id"], {"players": [], "best": None})
        t["players"].append(r["account_id"])
        best = t["best"]
        if best is None or r["human_placement"] < best:
            t["best"] = r["human_placement"]

    out: list[MatchGroup] = []
    for match_id, m in by_match.items():
        # A team's result is its best human finish. Rank teams among themselves so
        # the ranking is dense and human-relative regardless of bot padding.
        ordered = sorted(m["teams"].items(), key=lambda kv: kv[1]["best"])
        teams: list[tuple[int, int, list[str]]] = []
        rank = 0
        prev_best = None
        for i, (team_id, t) in enumerate(ordered):
            if t["best"] != prev_best:
                rank = i + 1
                prev_best = t["best"]
            teams.append((team_id, rank, sorted(t["players"])))
        out.append(MatchGroup(match_id, m["ts"], teams))

    # Deterministic chronological order; match_id breaks ties reproducibly.
    out.sort(key=lambda g: (g.match_start_ts, g.match_id))
    return out


def run_ratings(groups: list[MatchGroup]) -> list[dict]:
    """Sequential fold over matches. Returns one row per rated player per match."""
    model = PlackettLuce()
    state: dict[str, Any] = {}
    games: dict[str, int] = {}
    updates: list[dict] = []

    for g in groups:
        # A single human team has nothing to be compared against.
        if len(g.teams) < 2:
            continue

        rosters = [players for _, _, players in g.teams]
        ranks = [rank for _, rank, _ in g.teams]

        pre = [[state.get(p) or model.rating(name=p) for p in players] for players in rosters]
        post = model.rate(pre, ranks=ranks)

        for (team_id, rank, players), before, after in zip(g.teams, pre, post, strict=True):
            for account_id, b, a in zip(players, before, after, strict=True):
                updates.append(
                    {
                        "account_id": account_id,
                        "match_id": g.match_id,
                        "match_start_ts": g.match_start_ts,
                        "team_id": team_id,
                        "team_rank": rank,
                        "teams_in_match": len(g.teams),
                        "games_played_before": games.get(account_id, 0),
                        "mu_pre": b.mu,
                        "sigma_pre": b.sigma,
                        "ordinal_pre": b.ordinal(),
                        "mu_post": a.mu,
                        "sigma_post": a.sigma,
                        "ordinal_post": a.ordinal(),
                        "mu_delta": a.mu - b.mu,
                        "sigma_delta": a.sigma - b.sigma,
                        "rating_system": MODEL_VERSION,
                    }
                )
                state[account_id] = a
                games[account_id] = games.get(account_id, 0) + 1

    return updates


def write_updates(updates: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "fact_rating_update.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    pl.DataFrame(updates, infer_schema_length=None).write_parquet(tmp, compression="zstd")
    tmp.replace(path)
    return path
