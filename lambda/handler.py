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

## Why there are two discovery mechanisms

`/samples` alone stalls. It returns a pool that only refreshes periodically, so
once the queue drains the collector spends every invocation rediscovering ids it
already has — observed here as eleven consecutive runs reporting
`{"discovered": 0, "attempted": 0, "queued": 0}` while being perfectly healthy.

So the collector also walks **player histories**. Every fetched match names its
participants; a sample of those accounts is recorded, and later runs call
`/players` to pull each account's own recent matches. That closes the loop —
matches yield players, players yield matches — and it is what took the local
corpus from 3,556 to 31,875. Breadth from `/samples`, depth from `/players`.
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
PLAYERS_TABLE = os.environ.get("PLAYERS_TABLE", "")
# /players is rate limited (10 req/min) and takes 10 accounts per call, so five
# batches per invocation expands 50 players while leaving headroom for /samples.
COHORT_BATCHES = int(os.environ.get("COHORT_BATCHES", "5"))
# Seeds needed, not a census: three accounts per match is plenty to keep the loop
# fed, and avoids ~60 conditional writes per match.
PLAYERS_PER_MATCH = int(os.environ.get("PLAYERS_PER_MATCH", "3"))
# Backpressure. One cohort batch discovers ~8,600 matches; one invocation fetches
# ~1,500. Discovering faster than you can fetch is not progress — PUBG deletes
# matches after 14 days, so an over-deep queue converts directly into `gone` rows.
# Above this many pending, discovery pauses and the run spends its time fetching.
COHORT_PAUSE_ABOVE = int(os.environ.get("COHORT_PAUSE_ABOVE", "20000"))


def require_env() -> None:
    missing = [
        n
        for n, v in (
            ("BUCKET", BUCKET),
            ("LEDGER_TABLE", TABLE),
            ("API_KEY_PARAM", API_KEY_PARAM),
            ("PLAYERS_TABLE", PLAYERS_TABLE),
        )
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


def pending_count(cap: int) -> int:
    """How many matches are queued, counted up to `cap` and no further.

    Bounded on purpose: the answer only gates a yes/no decision, so paging
    through a 500k-item index to get an exact number would cost more than the
    decision is worth.
    """
    total = 0
    kwargs: dict = {
        "TableName": TABLE,
        "IndexName": "status_index",
        "KeyConditionExpression": "#s = :p",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":p": {"S": "pending"}},
        "Select": "COUNT",
    }
    while total < cap:
        resp = client("dynamodb").query(**kwargs)
        total += resp.get("Count", 0)
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return total


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


# ------------------------------------------------------------------ players


def is_human(account_id: str | None) -> bool:
    """Bots are `ai.NNNN` and have no match history worth walking."""
    return bool(account_id) and account_id.startswith("account.")


def participants(match: dict) -> list[str]:
    """Human account ids named in a match payload."""
    out = []
    for item in match.get("included", []):
        if item.get("type") != "participant":
            continue
        acct = item.get("attributes", {}).get("stats", {}).get("playerId")
        if is_human(acct):
            out.append(acct)
    return out


def register_players(account_ids: list[str]) -> int:
    """Record accounts as candidates for expansion. Idempotent by condition.

    The condition matters more than it looks: without it a re-seen account would
    be written back as `new`, and the collector would expand the same handful of
    players forever instead of moving through the population.
    """
    new = 0
    for acct in account_ids:
        try:
            client("dynamodb").put_item(
                TableName=PLAYERS_TABLE,
                Item={
                    "account_id": {"S": acct},
                    "expand_status": {"S": "new"},
                    "first_seen": {"S": now_iso()},
                },
                ConditionExpression="attribute_not_exists(account_id)",
            )
            new += 1
        except client("dynamodb").exceptions.ConditionalCheckFailedException:
            pass
    return new


def players_to_expand(limit: int) -> list[str]:
    resp = client("dynamodb").query(
        TableName=PLAYERS_TABLE,
        IndexName="expand_index",
        KeyConditionExpression="#s = :n",
        ExpressionAttributeNames={"#s": "expand_status"},
        ExpressionAttributeValues={":n": {"S": "new"}},
        Limit=min(limit, 100),
    )
    return [i["account_id"]["S"] for i in resp.get("Items", [])]


def mark_player_expanded(account_id: str, matches_found: int) -> None:
    client("dynamodb").update_item(
        TableName=PLAYERS_TABLE,
        Key={"account_id": {"S": account_id}},
        UpdateExpression="set #s = :d, expanded_at = :t, matches_found = :n",
        ExpressionAttributeNames={"#s": "expand_status"},
        ExpressionAttributeValues={
            ":d": {"S": "expanded"},
            ":t": {"S": now_iso()},
            ":n": {"N": str(matches_found)},
        },
    )


def fetch_player_matches(account_ids: list[str]) -> dict[str, list[str]]:
    """Match history for up to 10 accounts in one metered call."""
    url = f"{BASE}/players?filter[playerIds]={','.join(account_ids)}"
    payload = json.loads(_get(url))
    out: dict[str, list[str]] = {}
    for item in payload.get("data", []) or []:
        rel = item.get("relationships", {}).get("matches", {}).get("data") or []
        out[item["id"]] = [m["id"] for m in rel]
    return out


def expand_cohort() -> dict[str, int]:
    """Walk player histories to find matches /samples will never return.

    Skipped entirely when the queue is already deeper than the fetcher can drain
    before matches age out. Discovery is ~50x cheaper than fetching here, so
    without this the ledger fills with ids that expire unfetched.
    """
    queued = pending_count(COHORT_PAUSE_ABOVE)
    if queued >= COHORT_PAUSE_ABOVE:
        print(f"cohort paused: {queued}+ already pending")
        return {"players_expanded": 0, "cohort_discovered": 0, "cohort_paused": 1}

    discovered = players = 0
    for _ in range(COHORT_BATCHES):
        batch = players_to_expand(10)
        if not batch:
            break
        try:
            found = fetch_player_matches(batch)
        except FileNotFoundError:
            # Accounts can vanish — bans, renames. Mark them so the query moves on
            # instead of returning the same dead batch on every future run.
            for acct in batch:
                mark_player_expanded(acct, 0)
            continue
        except Exception as exc:  # noqa: BLE001 - discovery must not end the run
            print(f"cohort batch failed: {type(exc).__name__}: {exc}")
            break

        match_ids = sorted({m for ms in found.values() for m in ms})
        discovered += register(match_ids)
        for acct in batch:
            mark_player_expanded(acct, len(found.get(acct, [])))
            players += 1
    return {
        "players_expanded": players,
        "cohort_discovered": discovered,
        "cohort_paused": 0,
    }


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
        # Seed the next round of discovery from this match's roster.
        if PLAYERS_PER_MATCH:
            register_players(participants(match)[:PLAYERS_PER_MATCH])

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

    # Walk player histories. This is what keeps the collector fed once /samples
    # goes stale, which it does within a day or so.
    cohort = {"players_expanded": 0, "cohort_discovered": 0, "cohort_paused": 0}
    try:
        cohort = expand_cohort()
    except Exception as exc:  # noqa: BLE001 - fetching can still proceed
        print(f"cohort expansion failed: {type(exc).__name__}: {exc}")

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

    result = {
        "discovered": discovered,
        **cohort,
        "attempted": done,
        "queued": len(todo),
        **tally,
    }
    print(json.dumps(result))
    return result
