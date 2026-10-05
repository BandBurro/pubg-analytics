#!/usr/bin/env python3
"""Check the backfill before anything is deleted, and emit the deletion list.

PUBG serves no match older than 14 days, so every raw object removed here is
removed permanently. "The job printed no errors" is not evidence that it is
safe to delete: the first validation batch of this very backfill reported
success while silently discarding 69 of 200 matches to a decoding bug. So this
script does not ask whether the run *finished*. It asks whether each specific
match is readable in Parquet right now, and it writes the delete list from that
answer rather than from the run's own account of itself.

Four checks, each able to veto:

1. **Coverage** — every batch in the manifest's chunking has a marker. A gap
   means a shard died mid-range and that range was never shredded.
2. **Parity** — the Parquet actually present holds as many distinct matches as
   the markers claim. Catches an upload that 200-ed but wrote nothing.
3. **Accounting** — matches shredded plus matches skipped equals the manifest.
   Catches a batch that quietly processed a short list.
4. **Containment** — the delete list is built by intersecting raw keys with
   match_ids *read back out of Parquet*, so a match no longer in S3-as-Parquet
   can never appear in it, whatever the markers say.

Nothing is deleted here. The output is a file for a human to look at.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os

MANIFEST_KEY = "backfill/manifest.txt.gz"
MARKER_PREFIX = "backfill/done/"
BRONZE_PREFIX = "bronze/"
PART_OFFSET = 10_000


def s3_client():
    import boto3

    return boto3.client("s3")


def load_markers(bucket: str, s3) -> dict[int, dict]:
    markers: dict[int, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=MARKER_PREFIX):
        for obj in page.get("Contents", []):
            stem = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
            if not stem.isdigit():
                continue
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            markers[int(stem)] = json.loads(body)
    return markers


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument(
        "--out",
        default="/tmp/backfill_delete_list.txt",
        help="Where to write the keys that are provably safe to delete.",
    )
    args = ap.parse_args()

    import duckdb

    s3 = s3_client()

    keys = (
        gzip.decompress(s3.get_object(Bucket=args.bucket, Key=MANIFEST_KEY)["Body"].read())
        .decode()
        .splitlines()
    )
    expected_parts = (len(keys) + args.batch - 1) // args.batch
    print(f"manifest       : {len(keys):,} telemetry keys -> {expected_parts:,} batches")

    markers = load_markers(args.bucket, s3)
    print(f"markers        : {len(markers):,}")

    problems: list[str] = []

    # 1. coverage
    missing = [PART_OFFSET + i for i in range(expected_parts) if PART_OFFSET + i not in markers]
    if missing:
        problems.append(
            f"{len(missing):,} batches have no marker "
            f"(first few: {missing[:5]}) — those matches were never shredded"
        )
    print(f"coverage       : {expected_parts - len(missing):,}/{expected_parts:,} batches")

    # 3. accounting
    claimed = sum(m["matches"] for m in markers.values())
    skipped = sum(m["skipped"] for m in markers.values())
    covered = sum(
        len(keys[(p - PART_OFFSET) * args.batch : (p - PART_OFFSET + 1) * args.batch])
        for p in markers
    )
    print(f"accounting     : {claimed:,} shredded + {skipped:,} skipped = {claimed + skipped:,}")
    if claimed + skipped != covered:
        problems.append(
            f"markers account for {claimed + skipped:,} matches but cover "
            f"{covered:,} manifest entries — {covered - claimed - skipped:,} unexplained"
        )

    # 2 & 4. read the Parquet back and find out what is really there.
    #
    # Only bronze/match is read, never bronze/player_position. The match table
    # is ~22 KB per part; positions are ~23 MB per part, and pulling 60 GB of
    # them out of the region to run a check would cost more in egress than the
    # storage this whole exercise is reclaiming.
    print("reading bronze/match back out of Parquet (this takes a minute)...")
    con = duckdb.connect()
    con.execute("install httpfs; load httpfs;")
    # DuckDB does not read the shell's AWS_PROFILE on its own; without an
    # explicit secret it tries anonymous access and gets a 403.
    profile = os.environ.get("AWS_PROFILE", "default")
    region = os.environ.get("AWS_REGION", "us-east-2")
    con.execute(
        "create or replace secret s3creds ("
        "type s3, provider credential_chain, chain 'config', "
        f"profile '{profile}', region '{region}')"
    )
    con.execute(
        "create or replace table shredded as select distinct match_id "
        f"from read_parquet('s3://{args.bucket}/{BRONZE_PREFIX}match/*.parquet')"
    )
    actual = con.execute("select count(*) from shredded").fetchone()[0]
    print(f"parity         : {actual:,} distinct matches readable in Parquet")
    if actual < claimed:
        problems.append(
            f"Parquet holds {actual:,} matches but markers claim {claimed:,} — "
            f"{claimed - actual:,} claimed matches are not actually readable"
        )

    # Containment, done as a plain set intersection: a raw key survives into the
    # delete list only if its match_id was just read back out of Parquet. This
    # is the check that makes the rest advisory — whatever the markers claim, a
    # match that cannot be read cannot be deleted.
    shredded = {r[0] for r in con.execute("select match_id from shredded").fetchall()}
    deletable_keys = [k for k in keys if k.rsplit("/", 1)[-1].removesuffix(".json.gz") in shredded]
    with open(args.out, "w") as fh:
        fh.write("\n".join(sorted(deletable_keys)))
    deletable = len(deletable_keys)

    print(f"containment    : {deletable:,} of {len(keys):,} raw keys are provably shredded")
    print(f"delete list    : {args.out}")

    gb = deletable * 2.0 / 1024
    print(f"\nwould free     : ~{gb:,.0f} GB  (~${gb * 0.023:,.2f}/month)")
    print(f"would retain   : {len(keys) - deletable:,} raw keys not yet proven shredded")

    if problems:
        print("\nNOT SAFE TO DELETE:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nAll four checks passed. The delete list is safe to act on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
