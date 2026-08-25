"""Benchmark DuckDB on the position stream, to decide whether Spark is warranted.

The roadmap assumed the ~200M-row position layer was where a single machine gives
up and distributed compute earns its place. That assumption deserved measuring
rather than believing, because "I used Spark on data that fit in RAM" is a tell,
and knowing when *not* to reach for it is the more useful judgement.

Run: uv run python scripts/bench_positions.py
"""

import time
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parents[1] / "data" / "bronze"


def main() -> None:
    con = duckdb.connect(":memory:")
    con.execute("pragma disable_progress_bar")
    con.execute(
        f"create view pos as select match_id, event_ts::timestamp ts, account_id, "
        f"is_bot, x, y, z from read_parquet('{DATA}/player_position/*.parquet')"
    )
    con.execute(
        f"create view kil as select match_id, event_ts::timestamp ts, victim_account_id "
        f"from read_parquet('{DATA}/kill/*.parquet')"
    )

    rows = con.execute("select count(*) from pos").fetchone()[0]
    kills = con.execute("select count(*) from kil").fetchone()[0]
    print(f"position rows: {rows:,}    kills: {kills:,}\n")
    print(f"{'query':<50}{'wall':>9}")
    print("-" * 62)

    def timed(label: str, sql: str) -> float:
        start = time.perf_counter()
        con.execute(sql).fetchall()
        elapsed = time.perf_counter() - start
        print(f"{label:<50}{elapsed:>8.2f}s")
        return elapsed

    timed("full scan count", "select count(*) from pos")
    timed(
        "group by match",
        "select match_id, count(*) c from pos group by 1 order by c desc limit 1",
    )
    timed(
        "grid heatmap (bucket + aggregate)",
        "select cast(floor(x/50000) as int) gx, cast(floor(y/50000) as int) gy, "
        "count(*) n from pos where not is_bot group by 1,2 order by n desc limit 1",
    )
    # The expensive one: a window over every row, partitioned per player per match.
    timed(
        "rotation distance (window per player)",
        """
        with d as (
          select sqrt(pow(x - lag(x) over w, 2) + pow(y - lag(y) over w, 2)) step
          from pos where not is_bot
          window w as (partition by match_id, account_id order by ts)
        ) select count(*), sum(step) from d where step is not null
        """,
    )
    # The shuffle-heavy one: every kill against every position within 10s.
    timed(
        "kills x positions, +/-10s range join",
        """
        select count(*) from kil k join pos p
          on p.match_id = k.match_id
         and p.ts between k.ts - interval 10 second and k.ts + interval 10 second
        """,
    )

    print("\nskew of the range join (pairs per match):")
    con.execute(
        """
        create table pairs as
        select k.match_id, count(*) n from kil k join pos p
          on p.match_id = k.match_id
         and p.ts between k.ts - interval 10 second and k.ts + interval 10 second
        group by 1
        """
    )
    r = con.execute(
        """select count(*), min(n), avg(n), max(n), 1.0*max(n)/avg(n) from pairs"""
    ).fetchone()
    print(f"  matches={r[0]:,}  min={r[1]:,}  avg={r[2]:,.0f}  max={r[3]:,}  max/avg={r[4]:.1f}x")


if __name__ == "__main__":
    main()
