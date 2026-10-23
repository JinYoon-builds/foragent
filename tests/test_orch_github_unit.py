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
