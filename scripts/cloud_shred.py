#!/usr/bin/env python3
"""Shred raw telemetry into Parquet from *inside* us-east-2.

Why this exists as a separate program rather than a flag on `pubg shred`:

**Egress.** S3 charges $0.09/GB to move bytes out of the region, beyond a
100 GB/month allowance. The raw corpus is ~1,005 GB, so shredding it on a laptop
would cost roughly $80 in transfer alone — about three years of the storage bill
the shred is meant to eliminate. Compute placed next to the data costs ~$2.

The Parquet it produces measures 152 KB/match against 2.0 MB of raw telemetry,
a 12.9x reduction: ~78 GB for the 514,490-match corpus. That still fits under
the free egress allowance, so the result can be pulled down afterwards for
nothing — but only just, which is worth knowing before the corpus grows again.

**One parse, two shredders.** The local CLI runs `shred` and `shred-positions`
as separate passes, each gunzipping and parsing every telemetry file. That is
the right design locally, where the two passes have independent state and get
re-run independently. Here the corpus is read exactly once, so both shredders
are fed from a single parse.

**Idempotency without a ledger.** The cloud ledger lives in DynamoDB and the
local one in SQLite; introducing a third would mean three things to reconcile.
Instead the output itself is the state: batch *i* always covers the same
matches, and a marker object is written only after its Parquet lands. A rerun
lists the markers and skips that work. Spot interruption costs at most one
batch.

Part numbers start at PART_OFFSET so cloud parts can be synced into a local
`data/bronze/` without colliding with locally-produced ones.
"""

from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import orjson  # noqa: E402

from pubg_analytics.shred import (  # noqa: E402
    PositionShredder,
    Shredder,
    match_definition,
)

# Local parts reached 00187 after 64k matches, so 10000 leaves room for the
# local collector to keep running for years before the namespaces could meet.
PART_OFFSET = 10_000

TELEMETRY_PREFIX = "raw/telemetry/"
BRONZE_PREFIX = "bronze/"
MARKER_PREFIX = "backfill/done/"
MANIFEST_KEY = "backfill/manifest.txt.gz"

# Downloads happen in sub-chunks so a worker never holds a whole batch of
# compressed telemetry in memory at once: 2.1 MB average * 200 would be 428 MB
# per worker before any shredding starts.
DOWNLOAD_CHUNK = 25
DOWNLOAD_THREADS = 16


def s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        config=Config(
            retries={"max_attempts": 10, "mode": "adaptive"},
            max_pool_connections=DOWNLOAD_THREADS * 2,
        ),
    )


# ------------------------------------------------------------------ manifest


def build_manifest(bucket: str, s3) -> list[str]:
    """List every telemetry key once and cache it in S3.

    Listing ~500k keys takes minutes and costs LIST requests. The backfill may
    be restarted several times (spot interruption, a bug found mid-run), and
    re-listing each time would both cost money and, worse, produce a different
    ordering if new objects appeared — which would silently change what "batch
    7" means and break the idempotency markers.
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=MANIFEST_KEY)["Body"].read()
        keys = gzip.decompress(body).decode().splitlines()
        print(f"manifest: {len(keys):,} keys (cached)", flush=True)
        return keys
    except s3.exceptions.NoSuchKey:
        pass

    print("manifest: listing telemetry keys (this takes a few minutes)...", flush=True)
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=TELEMETRY_PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json.gz"):
                keys.append(obj["Key"])
        if len(keys) % 100_000 < 1000:
            print(f"  ...{len(keys):,}", flush=True)

    keys.sort()
    s3.put_object(
        Bucket=bucket,
        Key=MANIFEST_KEY,
        Body=gzip.compress("\n".join(keys).encode(), 6),
    )
    print(f"manifest: {len(keys):,} keys (built and cached)", flush=True)
    return keys


def check_batch_size(bucket: str, s3, batch: int) -> None:
    """Pin the batch size for the life of the backfill.

    Part numbers are positions in a fixed chunking of the manifest, so batch 200
    and batch 100 disagree about which matches "part 10000" covers. A marker
    written under one size would then cause the other to skip a range it never
    actually shredded — silent, permanent data loss that no error surfaces. The
    first run records the size; later runs must match it or refuse to start.
    """
    key = "backfill/batch_size"
    try:
        recorded = int(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        s3.put_object(Bucket=bucket, Key=key, Body=str(batch).encode())
        return
    if recorded != batch:
        raise SystemExit(
            f"this backfill was started with --batch {recorded}; refusing to run "
            f"with --batch {batch}. Markers are only meaningful at a fixed size. "
            f"Use --batch {recorded}, or delete s3://{bucket}/backfill/ to restart."
        )


def done_parts(bucket: str, s3) -> set[int]:
    done: set[int] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=MARKER_PREFIX):
        for obj in page.get("Contents", []):
            stem = obj["Key"].rsplit("/", 1)[-1].removesuffix(".json")
            if stem.isdigit():
                done.add(int(stem))
    return done


# ------------------------------------------------------------------ worker


def _fetch(s3, bucket: str, key: str) -> bytes | None:
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return None


def _parse(raw: bytes):
    """Unwrap gzip until the payload stops being gzip, then parse.

    An early version of the cloud collector compressed a payload the CDN had
    already compressed, so roughly a third of the corpus is double-gzipped.
    `repair-gzip` fixed the *local* copies; the S3 originals were never touched,
    and a single decompress leaves them as gzip bytes that fail to parse as
    JSON. Looping on the magic bytes repairs both shapes on read without
    needing to know which objects are affected — and the raw objects stay
    untouched, so this is a read-side fix, not a migration.
    """
    while raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return orjson.loads(raw)


def process_batch(args: tuple[str, int, list[str]]) -> dict:
    """Shred one batch and upload its Parquet. Returns a summary dict.

    Failures are counted and reported rather than raised: one unreadable
    telemetry file should not cost the other 199 matches in the batch, and the
    raw object stays in place so a later pass can retry it.
    """
    bucket, part, keys = args
    s3 = s3_client()
    started = time.time()

    with TemporaryDirectory() as tmp:
        out = Path(tmp)
        sh = Shredder(out)
        ps = PositionShredder(out)
        ok = 0
        skipped: list[str] = []
        pos_rows = 0

        for i in range(0, len(keys), DOWNLOAD_CHUNK):
            chunk = keys[i : i + DOWNLOAD_CHUNK]
            manifest_keys = [k.replace("/telemetry/", "/matches/") for k in chunk]

            with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as pool:
                tele_raw = list(pool.map(lambda k: _fetch(s3, bucket, k), chunk))
                man_raw = list(pool.map(lambda k: _fetch(s3, bucket, k), manifest_keys))

            for key, t_raw, m_raw in zip(chunk, tele_raw, man_raw, strict=True):
                match_id = key.rsplit("/", 1)[-1].removesuffix(".json.gz")
                if t_raw is None or m_raw is None:
                    skipped.append(match_id)
                    continue
                try:
                    events = _parse(t_raw)
                    manifest = _parse(m_raw)
                    tele_mid, ping = match_definition(events)
                    sh.add_manifest(manifest, tele_mid, ping)
                    sh.add_telemetry(match_id, events)
                    pos_rows += ps.add(match_id, events)
                    ok += 1
                except (OSError, orjson.JSONDecodeError, KeyError, TypeError) as exc:
                    skipped.append(f"{match_id}:{type(exc).__name__}")

        written = sh.flush(part)
        written_pos = ps.flush(part)

        uploaded = 0
        for table_dir in sorted(out.iterdir()):
            if not table_dir.is_dir():
                continue
            for pq in table_dir.glob("*.parquet"):
                s3.upload_file(str(pq), bucket, f"{BRONZE_PREFIX}{table_dir.name}/{pq.name}")
                uploaded += 1

    summary = {
        "part": part,
        "matches": ok,
        "skipped": len(skipped),
        "skipped_ids": skipped[:20],
        "position_rows": written_pos,
        "tables": written,
        "files": uploaded,
        "seconds": round(time.time() - started, 1),
    }
    # The marker is written last and only here: its presence is the sole
    # assertion that this batch's Parquet is durably in S3.
    s3.put_object(
        Bucket=bucket,
        Key=f"{MARKER_PREFIX}{part:05d}.json",
        Body=json.dumps(summary).encode(),
    )
    print(
        f"  part {part:05d}: {ok:4d} matches, {written_pos:>9,} pos rows, "
        f"{summary['skipped']:3d} skipped, {summary['seconds']:6.1f}s",
        flush=True,
    )
    return summary


# ------------------------------------------------------------------ driver


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", default=os.environ.get("BUCKET", ""))
    ap.add_argument("--batch", type=int, default=200, help="Matches per Parquet part.")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--limit", type=int, default=0, help="Max batches; 0 = all.")
    ap.add_argument("--dry-run", action="store_true", help="Plan only, shred nothing.")
    # Sharding exists because the account's Free Plan only permits free-tier
    # instance types, all of which are 2 vCPU. Scaling up is not available, so
    # the job scales out instead: N machines each take every Nth batch. Batches
    # are already independent and deterministic, so no coordination is needed
    # and the total cost is unchanged — only the wall clock divides.
    ap.add_argument("--shard", type=int, default=0, help="This worker's index.")
    ap.add_argument("--shards", type=int, default=1, help="Total workers.")
    args = ap.parse_args()

    if not 0 <= args.shard < args.shards:
        print(f"--shard must be in [0,{args.shards})", file=sys.stderr)
        return 2

    if not args.bucket:
        print("need --bucket or BUCKET", file=sys.stderr)
        return 2

    s3 = s3_client()
    check_batch_size(args.bucket, s3, args.batch)
    keys = build_manifest(args.bucket, s3)
    if not keys:
        print("no telemetry found", file=sys.stderr)
        return 1

    batches = [
        (args.bucket, PART_OFFSET + i, keys[s : s + args.batch])
        for i, s in enumerate(range(0, len(keys), args.batch))
    ]
    already = done_parts(args.bucket, s3)
    todo = [b for b in batches if b[1] not in already]
    if args.shards > 1:
        todo = [b for b in todo if b[1] % args.shards == args.shard]
    if args.limit:
        todo = todo[: args.limit]

    print(
        f"{len(keys):,} matches in {len(batches):,} batches of {args.batch}; "
        f"{len(already):,} already done; {len(todo):,} to do "
        f"(shard {args.shard}/{args.shards}); {args.workers} workers",
        flush=True,
    )
    if args.dry_run or not todo:
        return 0

    started = time.time()
    matches = pos = skipped = 0
    with mp.Pool(args.workers) as pool:
        for n, r in enumerate(pool.imap_unordered(process_batch, todo), 1):
            matches += r["matches"]
            pos += r["position_rows"]
            skipped += r["skipped"]
            if n % 25 == 0:
                rate = matches / (time.time() - started)
                left = (len(todo) - n) * args.batch / rate / 3600 if rate else 0
                print(
                    f"-- {n}/{len(todo)} batches | {matches:,} matches | "
                    f"{pos:,} pos rows | {rate:.1f}/s | ~{left:.1f}h left",
                    flush=True,
                )

    elapsed = time.time() - started
    print(
        f"\ndone: {matches:,} matches, {pos:,} position rows, {skipped:,} skipped, "
        f"in {elapsed / 3600:.2f}h ({matches / elapsed:.1f}/s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
