"""Scheduled PUBG collector, running in Lambda.

Deliberately **standard library only** — no httpx, no orjson, no boto3 vendoring.
Two reasons:

1. `orjson` and friends ship compiled wheels. Building them for Lambda's Linux
   ARM runtime from a macOS laptop means cross-compilation, a layer, or a
   container image. All three are real work that buys nothing here: this function
   fetches JSON and writes bytes.
2. A zero-dependency function is a ~10 KB zip that deploys in seconds and cannot
   break because a wheel didn't match the runtime.

`boto3` is the exception, and it is already present in the Lambda runtime.

Differences from the local collector, all forced by the environment:

* **State lives in DynamoDB, not SQLite.** Lambda's filesystem is ephemeral and
  not shared between invocations, so a local ledger file would forget everything
  each run.
* **Concurrency comes from threads, not asyncio.** Same effect, no dependency.
* **One invocation is bounded by the 15-minute timeout**, so it takes a slice of
  the queue and leaves the rest for the next run. The ledger makes that safe.
"""

import concurrent.futures
import gzip
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

import boto3

# Read with defaults rather than indexing, so the module imports cleanly for
# tests and a misconfiguration surfaces as a clear error inside the handler
# instead of an opaque cold-start import failure.
BUCKET = os.environ.get("BUCKET", "")
TABLE = os.environ.get("LEDGER_TABLE", "")
SHARD = os.environ.get("SHARD", "steam")
API_KEY_PARAM = os.environ.get("API_KEY_PARAM", "")
MAX_FETCH = int(os.environ.get("MAX_FETCH", "800"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "8"))


def require_env() -> None:
    missing = [
        n
        for n, v in (("BUCKET", BUCKET), ("LEDGER_TABLE", TABLE), ("API_KEY_PARAM", API_KEY_PARAM))
        if not v
    ]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")


# Leave headroom before the hard timeout so in-flight writes finish cleanly.
DEADLINE_BUFFER_S = 90

JSONAPI = "application/vnd.api+json"
BASE = f"https://api.pubg.com/shards/{SHARD}"

# Created lazily: constructing clients at import time makes the module
# unimportable without AWS credentials, which breaks local testing.
_clients: dict[str, object] = {}
_api_key: str | None = None


def client(name: str):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def api_key() -> str:
    """Read the key once per container, not once per request."""
    global _api_key
    if _api_key is None:
        _api_key = client("ssm").get_parameter(Name=API_KEY_PARAM, WithDecryption=True)[
            "Parameter"
        ]["Value"]
    return _api_key


def _get(url: str, accept: str = JSONAPI, retries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {api_key()}", "Accept": accept}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from None
            if exc.code == 429:
                # Respect the server's reset hint when it offers one.
                reset = exc.headers.get("X-RateLimit-Reset")
                delay = 60.0
                if reset and str(reset).isdigit():
                    delay = max(1.0, int(reset) - time.time())
                time.sleep(min(delay, 90.0))
                last = exc
                continue
            if exc.code < 500:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(2**attempt)
    raise last or RuntimeError(f"exhausted retries for {url}")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ------------------------------------------------------------------ ledger


def register(match_ids: list[str]) -> int:
    """Record newly discovered ids. The condition expression makes this idempotent."""
    new = 0
    for mid in match_ids:
        try:
            client("dynamodb").put_item(
                TableName=TABLE,
                Item={
                    "match_id": {"S": mid},
                    "status": {"S": "pending"},
                    "discovered_at": {"S": now_iso()},
                    "attempts": {"N": "0"},
                },
                ConditionExpression="attribute_not_exists(match_id)",
            )
            new += 1
        except client("dynamodb").exceptions.ConditionalCheckFailedException:
            pass
    return new


def pending(limit: int) -> list[str]:
    """Oldest pending matches first, via the status index."""
    out: list[str] = []
    kwargs = {
        "TableName": TABLE,
        "IndexName": "status_index",
        "KeyConditionExpression": "#s = :p",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":p": {"S": "pending"}},
        "Limit": min(limit, 1000),
    }
    while len(out) < limit:
        resp = client("dynamodb").query(**kwargs)
        out.extend(i["match_id"]["S"] for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp or not resp.get("Items"):
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return out[:limit]


def mark(match_id: str, status: str, **extra: str) -> None:
    names = {"#s": "status"}
    values = {":s": {"S": status}, ":t": {"S": now_iso()}}
    sets = ["#s = :s", "fetched_at = :t"]
    for i, (k, v) in enumerate(extra.items()):
        names[f"#k{i}"] = k
        values[f":v{i}"] = {"S": str(v)[:400]}
        sets.append(f"#k{i} = :v{i}")
    client("dynamodb").update_item(
        TableName=TABLE,
        Key={"match_id": {"S": match_id}},
        UpdateExpression="set " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# ------------------------------------------------------------------ fetching


def telemetry_url(match: dict) -> str | None:
    for item in match.get("included", []):
        if item.get("type") == "asset":
            return item.get("attributes", {}).get("URL")
    return None


def put_gz(key: str, payload: bytes) -> None:
    client("s3").put_object(Bucket=BUCKET, Key=key, Body=gzip.compress(payload, 6))


def fetch_one(match_id: str) -> str:
    try:
        raw_match = _get(f"{BASE}/matches/{match_id}")
        match = json.loads(raw_match)
        attrs = match.get("data", {}).get("attributes", {})
        dt = (attrs.get("createdAt") or "unknown")[:10]
        url = telemetry_url(match)
        if not url:
            mark(match_id, "failed", error="no telemetry asset")
            return "no-telemetry"

        raw_tele = _get(url, accept="application/json")
        prefix = f"raw/{{kind}}/shard={SHARD}/dt={dt}/{match_id}.json.gz"
        put_gz(prefix.format(kind="matches"), raw_match)
        put_gz(prefix.format(kind="telemetry"), raw_tele)
        mark(match_id, "done", s3_key=prefix.format(kind="telemetry"))
        return "ok"
    except FileNotFoundError:
        # Aged out of the API — never retry it.
        mark(match_id, "gone", error="404")
        return "gone"
    except Exception as exc:  # noqa: BLE001 - one bad match must not end the run
        # Left pending on purpose: a transport failure says nothing about the
        # match, and burning its retry budget would discard a good one.
        mark(match_id, "pending", error=f"{type(exc).__name__}: {exc}")
        return "error"


def handler(event, context):
    require_env()
    deadline = time.time() + (context.get_remaining_time_in_millis() / 1000) - DEADLINE_BUFFER_S

    discovered = 0
    try:
        payload = json.loads(_get(f"{BASE}/samples"))
        rels = payload.get("data", {}).get("relationships", {})
        ids = [m["id"] for m in rels.get("matches", {}).get("data", [])]
        discovered = register(ids)
    except Exception as exc:  # noqa: BLE001 - fetching can still proceed
        print(f"discover failed: {type(exc).__name__}: {exc}")

    todo = pending(MAX_FETCH)
    tally: dict[str, int] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(fetch_one, mid): mid for mid in todo}
        for fut in concurrent.futures.as_completed(futures):
            r = fut.result()
            tally[r] = tally.get(r, 0) + 1
            done += 1
            if time.time() > deadline:
                # Stop cleanly and let the next scheduled run continue.
                print("approaching timeout; stopping early")
                for f in futures:
                    f.cancel()
                break

    result = {"discovered": discovered, "attempted": done, "queued": len(todo), **tally}
    print(json.dumps(result))
    return result
