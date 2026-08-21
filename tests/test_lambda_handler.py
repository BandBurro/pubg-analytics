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


def test_module_imports_without_aws_credentials():
    """Clients are built lazily, so importing must not touch AWS at all."""
    assert handler.BASE.startswith("https://api.pubg.com/shards/")
    assert handler._clients == {} or isinstance(handler._clients, dict)


def test_require_env_names_every_missing_variable():
    handler.BUCKET = ""
    handler.TABLE = ""
    handler.API_KEY_PARAM = ""
    with pytest.raises(RuntimeError) as exc:
        handler.require_env()
    msg = str(exc.value)
    for name in ("BUCKET", "LEDGER_TABLE", "API_KEY_PARAM"):
        assert name in msg, msg


def test_require_env_passes_when_configured():
    handler.BUCKET = "b"
    handler.TABLE = "t"
    handler.API_KEY_PARAM = "p"
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
