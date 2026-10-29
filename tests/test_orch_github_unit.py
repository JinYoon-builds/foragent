"""Reset lookup failures remain availability waits rather than issue failures."""

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from foragent import orch_github
from foragent.state import StateStore
from foragent.tracker import GhTracker, TrackerError


@pytest.mark.parametrize("response", [
    TrackerError("HTTP 503 unavailable"), "invalid json", "[]", "null",
    json.dumps({"resources": {"core": {"remaining": 1, "reset": 0}}}),
    json.dumps({"resources": {"core": {"remaining": 0, "reset": 10 ** 30}}}),
])
def test_missing_or_unreadable_reset_defers_without_failing(tmp_path, response):
    now = datetime(2026, 10, 23, tzinfo=UTC)
    calls = []

    def run(args, input=None):
        calls.append(args)
        if isinstance(response, Exception):
            raise response
        return response

    ctx = SimpleNamespace(tracker=GhTracker("owner/repo", run),
                          store=StateStore(tmp_path, now=lambda: now), now=lambda: now)
    until = orch_github.pause(ctx, TrackerError("API rate limit exceeded (HTTP 403)"))
    assert until == now + timedelta(minutes=1)
    assert calls == [["api", "rate_limit"]]
    assert StateStore(tmp_path).github_wait_until(now) == until
    assert ctx.store.load_flag(1) is None


def test_rate_limit_probe_saves_core_remaining_with_local_arrival_time(tmp_path):
    now = datetime(2026, 10, 29, tzinfo=UTC)
    calls = []

    def run(args, input=None):
        calls.append(args)
        return json.dumps({"resources": {"core": {"remaining": 0, "reset": int((now + timedelta(minutes=5)).timestamp())},
                                         "search": {"remaining": 9, "reset": 0}}})

    ctx = SimpleNamespace(tracker=GhTracker("owner/repo", run),
                          store=StateStore(tmp_path, now=lambda: now), now=lambda: now)
    orch_github.pause(ctx, TrackerError("API rate limit exceeded (HTTP 403)"))
    assert calls == [["api", "rate_limit"]], "The remaining count comes from the existing probe, not a new call"
    assert json.loads((tmp_path / "github-rate-limit.json").read_text()) == {"remaining": 0, "seen_at": now.isoformat()}


def test_failed_rate_limit_probe_keeps_the_previous_reading(tmp_path):
    now = datetime(2026, 10, 29, tzinfo=UTC)
    store = StateStore(tmp_path, now=lambda: now)
    store.record_github_remaining(42, now - timedelta(hours=1))
    before = (tmp_path / "github-rate-limit.json").read_text()

    def run(args, input=None):
        raise TrackerError("HTTP 503 unavailable")

    tracker = GhTracker("owner/repo", run)
    tracker.rate_limit_remaining = 7  # A stale value from an earlier probe must not be saved.
    orch_github.pause(SimpleNamespace(tracker=tracker, store=store, now=lambda: now), TrackerError("API rate limit"))
    assert (tmp_path / "github-rate-limit.json").read_text() == before
