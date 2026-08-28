"""Tests for the Lambda collector's pure logic.

A syntax error or a bad env-var contract in a Lambda normally surfaces only after
deploy, as an opaque cold-start failure in CloudWatch. These tests catch both on
the laptop, which is the entire reason the module was written to import without
AWS credentials.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

import handler  # noqa: E402

# These tests exercise module-level config, so without isolation one test's
# mutation leaks into the next and failures depend on collection order.
CONFIG_ATTRS = ("BUCKET", "TABLE", "API_KEY_PARAM", "PLAYERS_TABLE")


@pytest.fixture(autouse=True)
def restore_config():
    saved = {a: getattr(handler, a) for a in CONFIG_ATTRS}
    yield
    for a, v in saved.items():
        setattr(handler, a, v)


def test_module_imports_without_aws_credentials():
    """Clients are built lazily, so importing must not touch AWS at all."""
    assert handler.BASE.startswith("https://api.pubg.com/shards/")
    assert handler._clients == {} or isinstance(handler._clients, dict)


def test_require_env_names_every_missing_variable():
    handler.BUCKET = ""
    handler.TABLE = ""
    handler.API_KEY_PARAM = ""
    handler.PLAYERS_TABLE = ""
    with pytest.raises(RuntimeError) as exc:
        handler.require_env()
    msg = str(exc.value)
    for name in ("BUCKET", "LEDGER_TABLE", "API_KEY_PARAM", "PLAYERS_TABLE"):
        assert name in msg, msg


def test_require_env_passes_when_configured():
    handler.BUCKET = "b"
    handler.TABLE = "t"
    handler.API_KEY_PARAM = "p"
    handler.PLAYERS_TABLE = "pl"
    handler.require_env()  # must not raise


def test_telemetry_url_extracts_the_asset():
    match = {
        "included": [
            {"type": "participant", "attributes": {}},
            {"type": "asset", "attributes": {"URL": "https://cdn/telemetry.json"}},
        ]
    }
    assert handler.telemetry_url(match) == "https://cdn/telemetry.json"


def test_telemetry_url_is_none_when_absent():
    # Some matches genuinely have no telemetry asset; that must not raise.
    assert handler.telemetry_url({"included": []}) is None
    assert handler.telemetry_url({}) is None


def test_s3_key_layout_matches_the_local_collector():
    """Cloud and local paths must agree, or the warehouse can't read both."""
    shard, match_id, dt = "steam", "abc-123", "2026-08-21"
    key = f"raw/telemetry/shard={shard}/dt={dt}/{match_id}.json.gz"
    assert key == f"raw/telemetry/shard=steam/dt=2026-08-21/{match_id}.json.gz"
    # Same partition scheme the local collector writes, so a single dbt source
    # can point at either.
    assert key.startswith("raw/telemetry/shard=")


# --- cohort discovery: the half that keeps working after /samples goes stale ---


def test_is_human_separates_accounts_from_bots():
    assert handler.is_human("account.abc123")
    assert not handler.is_human("ai.2840")
    assert not handler.is_human(None)
    assert not handler.is_human("")


def test_participants_returns_only_human_accounts():
    """Bots have no match history to walk, so seeding on them wastes API calls."""
    match = {
        "included": [
            {"type": "participant", "attributes": {"stats": {"playerId": "account.aaa"}}},
            {"type": "participant", "attributes": {"stats": {"playerId": "ai.9001"}}},
            {"type": "participant", "attributes": {"stats": {"playerId": "account.bbb"}}},
            {"type": "roster", "attributes": {"stats": {"playerId": "account.ccc"}}},
            {"type": "asset", "attributes": {"URL": "https://cdn/x.json"}},
        ]
    }
    assert handler.participants(match) == ["account.aaa", "account.bbb"]


def test_participants_tolerates_missing_structure():
    assert handler.participants({}) == []
    assert handler.participants({"included": [{"type": "participant"}]}) == []
    assert handler.participants({"included": [{"type": "participant", "attributes": {}}]}) == []


def test_require_env_now_demands_the_players_table():
    handler.BUCKET, handler.TABLE, handler.API_KEY_PARAM = "b", "t", "p"
    handler.PLAYERS_TABLE = ""
    with pytest.raises(RuntimeError) as exc:
        handler.require_env()
    assert "PLAYERS_TABLE" in str(exc.value)
    handler.PLAYERS_TABLE = "pl"
    handler.require_env()  # must not raise


# --- the double-gzip bug: urllib does not decompress the way httpx does ---


def test_decode_body_unwraps_gzip_by_header():
    import gzip as gz

    payload = b'[{"_T":"LogMatchStart"}]'
    assert handler._decode_body(gz.compress(payload), "gzip") == payload


def test_decode_body_unwraps_gzip_without_the_header():
    """PUBG's CDN serves compressed telemetry without always advertising it."""
    import gzip as gz

    payload = b'[{"_T":"LogMatchStart"}]'
    assert handler._decode_body(gz.compress(payload), None) == payload


def test_decode_body_passes_plain_json_through():
    payload = b'{"data":{"id":"abc"}}'
    assert handler._decode_body(payload, None) == payload
    assert handler._decode_body(payload, "identity") == payload


def test_decode_body_survives_a_false_gzip_signature():
    """Magic bytes that aren't really gzip must not raise mid-collection."""
    assert handler._decode_body(b"\x1f\x8b not really gzip", None) == b"\x1f\x8b not really gzip"
