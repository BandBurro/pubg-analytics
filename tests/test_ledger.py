"""The ledger is what makes collection safe to re-run, so it gets tested first."""

from pubg_analytics.ledger import Ledger


def test_discovery_is_idempotent(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        assert led.add_discovered(["a", "b", "c"]) == 3
        # Re-discovering the same sample must add nothing.
        assert led.add_discovered(["a", "b", "c"]) == 0
        assert led.add_discovered(["c", "d"]) == 1
        assert led.stats()["total"] == 4


def test_done_matches_are_not_returned_as_pending(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        led.add_discovered(["a", "b"])
        assert set(led.pending(10)) == {"a", "b"}

        led.mark_done(
            "a",
            telemetry_path="/tmp/a.json.gz",
            event_count=42_000,
            match_created_at="2026-08-16T10:00:00Z",
            map_name="Baltic_Main",
            game_mode="squad-fpp",
        )
        assert led.pending(10) == ["b"]

        stats = led.stats()
        assert stats["done"] == 1
        assert stats["events"] == 42_000


def test_failures_retry_until_the_cap_then_stop(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        led.add_discovered(["a"])
        for _ in range(3):
            assert led.pending(10, max_attempts=3) == ["a"]
            led.mark_failed("a", "boom")
        # Fourth attempt is refused — a permanently broken match can't wedge the queue.
        assert led.pending(10, max_attempts=3) == []


def test_aged_out_matches_are_never_retried(tmp_path):
    with Ledger(tmp_path / "l.sqlite") as led:
        led.add_discovered(["a"])
        led.mark_failed("a", "404", gone=True)
        assert led.pending(10) == []
        assert led.stats()["gone"] == 1


def test_network_failures_do_not_burn_the_attempt_budget(tmp_path):
    """A firewall in the way must not permanently discard good matches."""
    with Ledger(tmp_path / "l.sqlite") as led:
        led.add_discovered(["a"])
        for _ in range(10):
            led.mark_retryable("a", "ConnectError: CERTIFICATE_VERIFY_FAILED")
            # Still eligible, however many times the network fails.
            assert led.pending(10, max_attempts=3) == ["a"]
        assert led.stats()["pending"] == 1
