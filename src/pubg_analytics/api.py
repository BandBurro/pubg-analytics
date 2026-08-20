"""Async client for the PUBG developer API.

Two classes of endpoint, deliberately handled differently:

* **Metered** — `/samples`, `/players`, `/seasons`. Capped at 10 req/min on a
  free key, so these go through a sliding-window rate limiter.
* **Exempt** — `/matches/{id}` and telemetry asset downloads. Per PUBG's docs
  these do not count against the key's rate limit, so they get a plain
  concurrency cap instead. This is the whole reason bulk collection is viable.
"""

import asyncio
import time
from collections import deque

import httpx
import orjson

JSONAPI = "application/vnd.api+json"


class MatchGone(Exception):
    """Match is no longer retrievable — it aged out of the API."""


class SlidingWindowLimiter:
    """Allow at most `rpm` acquisitions in any rolling 60s window."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.rpm:
                    self._calls.append(now)
                    return
                await asyncio.sleep(60.0 - (now - self._calls[0]) + 0.05)


class PubgClient:
    def __init__(self, *, api_key: str, base_url: str, rpm: int = 10, concurrency: int = 8):
        self.base_url = base_url.rstrip("/")
        self._limiter = SlidingWindowLimiter(rpm)
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Accept": JSONAPI},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PubgClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(
        self, url: str, *, metered: bool, headers: dict[str, str] | None = None, retries: int = 4
    ) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(retries):
            if metered:
                await self._limiter.acquire()
            async with self._sem:
                try:
                    resp = await self._client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    last = exc
                    await asyncio.sleep(2**attempt)
                    continue

            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                raise MatchGone(f"404 for {url}")
            if resp.status_code == 429:
                # Honour the server's own reset hint when it gives one.
                reset = resp.headers.get("X-RateLimit-Reset")
                delay = 60.0
                if reset and reset.isdigit():
                    delay = max(1.0, int(reset) - time.time())
                await asyncio.sleep(min(delay, 120.0))
                continue
            if resp.status_code >= 500:
                last = httpx.HTTPStatusError(
                    f"{resp.status_code} from {url}", request=resp.request, response=resp
                )
                await asyncio.sleep(2**attempt)
                continue
            resp.raise_for_status()

        raise last or RuntimeError(f"exhausted retries for {url}")

    async def sample_match_ids(self, since: str | None = None) -> list[str]:
        """Bulk match discovery. `since` is an ISO8601 UTC timestamp."""
        url = f"{self.base_url}/samples"
        if since:
            url += f"?filter[createdAt-start]={since}"
        resp = await self._get(url, metered=True)
        payload = orjson.loads(resp.content)
        rels = payload.get("data", {}).get("relationships", {})
        return [m["id"] for m in rels.get("matches", {}).get("data", [])]

    async def get_players_matches(self, account_ids: list[str]) -> dict[str, list[str]]:
        """Match history for up to 10 accounts in one metered call.

        This is the endpoint that makes skill rating possible. /samples returns
        random matches across the whole player base, so the same player almost
        never recurs and ratings never leave their prior. Fetching *players\'*
        histories instead produces the repeated observations a rating system needs.
        """
        if len(account_ids) > 10:
            raise ValueError("the API accepts at most 10 player ids per request")
        ids = ",".join(account_ids)
        url = f"{self.base_url}/players?filter[playerIds]={ids}"
        resp = await self._get(url, metered=True)
        payload = orjson.loads(resp.content)
        out: dict[str, list[str]] = {}
        for item in payload.get("data", []) or []:
            rel = item.get("relationships", {}).get("matches", {}).get("data") or []
            out[item["id"]] = [m["id"] for m in rel]
        return out

    async def get_match(self, match_id: str) -> dict:
        """Match detail: rosters, participants, and the telemetry asset link."""
        resp = await self._get(f"{self.base_url}/matches/{match_id}", metered=False)
        return orjson.loads(resp.content)

    async def download_telemetry(self, url: str) -> bytes:
        """Fetch the raw telemetry event array. Returns decoded JSON bytes."""
        # The CDN serves plain JSON and rejects the vnd.api+json Accept header.
        resp = await self._get(url, metered=False, headers={"Accept": "application/json"})
        return resp.content


def extract_telemetry_url(match_payload: dict) -> str | None:
    for item in match_payload.get("included", []):
        if item.get("type") == "asset":
            return item.get("attributes", {}).get("URL")
    return None


def extract_match_meta(match_payload: dict) -> dict[str, str | None]:
    attrs = match_payload.get("data", {}).get("attributes", {})
    return {
        "created_at": attrs.get("createdAt"),
        "map_name": attrs.get("mapName"),
        "game_mode": attrs.get("gameMode"),
    }
