"""Bootstrap the cloud collector's player table from the local corpus.

The cloud collector discovers matches two ways: `/samples` for breadth, and
player-history expansion for depth. The second one is self-sustaining —
matches name players, players name more matches — but it cannot *start* on its
own. The players table fills from fetched matches, and matches only get fetched
once something is queued, so an empty table plus a stale `/samples` is a
collector that idles forever. That is exactly what was observed: eleven healthy
invocations reporting `{"discovered": 0, "attempted": 0, "queued": 0}`.

This seeds it once from the 900k accounts the local warehouse already knows,
preferring the ones with the richest history — those return the most matches per
metered `/players` call.

Run: uv run python scripts/seed_cloud_cohort.py [--limit 500]
"""

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

import boto3
import duckdb

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "data" / "warehouse.duckdb"
TABLE = os.environ.get("PLAYERS_TABLE", "pubg-analytics-players")
PROFILE = os.environ.get("AWS_PROFILE", "pubg-personal")
REGION = os.environ.get("AWS_REGION", "us-east-2")


def pick_seeds(limit: int) -> list[str]:
    """Humans with the deepest local history, which yield the most per API call."""
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    rows = con.execute(
        """
        select account_id
        from main_gold.dim_player
        where not is_bot and analytical_matches >= 2
        order by analytical_matches desc, career_kills desc
        limit ?
        """,
        [limit],
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    seeds = pick_seeds(args.limit)
    print(f"selected {len(seeds):,} seed accounts from the local warehouse")

    ddb = boto3.Session(profile_name=PROFILE, region_name=REGION).client("dynamodb")
    now = datetime.now(UTC).isoformat()

    added = existing = 0
    for acct in seeds:
        try:
            # Conditional, not batch_write_item: batch writes cannot express
            # "only if absent", so re-running would reset already-expanded
            # accounts back to 'new' and the collector would loop over the same
            # players instead of advancing through the population.
            ddb.put_item(
                TableName=TABLE,
                Item={
                    "account_id": {"S": acct},
                    "expand_status": {"S": "new"},
                    "first_seen": {"S": now},
                },
                ConditionExpression="attribute_not_exists(account_id)",
            )
            added += 1
        except ddb.exceptions.ConditionalCheckFailedException:
            existing += 1

    print(f"added {added:,} new, {existing:,} already present")
    print("the collector will expand 50 of them per invocation (5 batches of 10)")


if __name__ == "__main__":
    main()
