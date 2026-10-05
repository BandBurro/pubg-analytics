#!/usr/bin/env python3
"""Delete raw telemetry that has been proven to exist as Parquet.

This is the irreversible step. PUBG serves no match older than 14 days, so
nothing removed here can be re-collected — not by re-running the collector, not
by paying, not ever. The corpus is the only copy.

So this program is deliberately incapable of deciding what to delete. It only
executes a list that `verify_backfill.py` produced by reading match ids back out
of Parquet, and it re-checks that list before acting:

* the list must be non-empty, or there is nothing to do and something is wrong;
* every key must sit under `raw/telemetry/`, so a malformed list cannot reach
  `raw/matches/` (the manifests, 5.7 GB, still needed to re-shred) or `bronze/`
  (the output this whole exercise exists to produce);
* every key must end in `.json.gz`, the shape the collector writes.

Any key failing those checks aborts the run rather than being skipped, because a
list containing something unexpected is a list that should not be trusted at all.

Dry-run is the default. Deleting requires --confirm, typed deliberately.
"""

from __future__ import annotations

import argparse
import sys

ALLOWED_PREFIX = "raw/telemetry/"
BATCH = 1000  # S3 DeleteObjects hard limit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--list", required=True, help="Delete list from verify_backfill.py.")
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this the run is a dry run.",
    )
    args = ap.parse_args()

    with open(args.list) as fh:
        keys = [line.strip() for line in fh if line.strip()]

    if not keys:
        print("delete list is empty — nothing to do", file=sys.stderr)
        return 1

    # Validate the whole list before touching anything. Checking as we go would
    # mean a bad entry halfway down is discovered only after the first half is
    # already gone.
    bad = [k for k in keys if not k.startswith(ALLOWED_PREFIX) or not k.endswith(".json.gz")]
    if bad:
        print(
            f"refusing to run: {len(bad):,} keys are not {ALLOWED_PREFIX}*.json.gz\n"
            f"  first offenders: {bad[:5]}",
            file=sys.stderr,
        )
        return 2

    gb = len(keys) * 2.0 / 1024
    print(f"bucket    : {args.bucket}")
    print(f"keys      : {len(keys):,}  (all under {ALLOWED_PREFIX})")
    print(f"frees     : ~{gb:,.0f} GB  (~${gb * 0.023:,.2f}/month)")

    if not args.confirm:
        print("\nDRY RUN — nothing deleted. Re-run with --confirm to delete.")
        print("First five keys that would go:")
        for k in keys[:5]:
            print(f"  {k}")
        return 0

    import boto3

    s3 = boto3.client("s3")
    deleted = errors = 0
    for i in range(0, len(keys), BATCH):
        chunk = keys[i : i + BATCH]
        resp = s3.delete_objects(
            Bucket=args.bucket,
            Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
        )
        deleted += len(chunk) - len(resp.get("Errors", []))
        for e in resp.get("Errors", []):
            errors += 1
            if errors <= 5:
                print(f"  error {e.get('Key')}: {e.get('Message')}", file=sys.stderr)
        if (i // BATCH) % 25 == 0:
            print(f"  {deleted:,}/{len(keys):,}", flush=True)

    print(f"\ndeleted {deleted:,} objects, {errors:,} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
