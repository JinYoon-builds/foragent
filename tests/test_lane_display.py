"""Additional coverage for optional lane names and inspect metadata."""

from datetime import UTC, datetime

import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.labels import lane_display


@pytest.mark.parametrize("lane,slug,language,expected", [
    ({}, "api", "en", "api"),
    ({"title": "Server"}, "api", "en", "Server"),
    ({"emoji": "🔧"}, "api", "en", "🔧 api"),
    ({}, "main", "ko-KR", "🧰 공통"),
    ({"title": "Shared", "emoji": "🌍"}, "main", "ko", "🌍 Shared"),
])
def test_optional_lane_names(lane, slug, language, expected):
    assert lane_display(Config(lanes={slug: lane}), slug, language) == expected


@pytest.mark.parametrize("key", ["title", "emoji"])
@pytest.mark.parametrize("value", [42, [], {}])
def test_lane_names_reject_non_strings(tmp_path, key, value):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"lanes": {"api": {key: value}}}))
    with pytest.raises(ConfigError, match=f"lanes.api.{key}"):
        load_config(tmp_path, global_path=tmp_path / "absent.yml")


def test_inspect_preserves_run_lane_for_closed_issue(tmp_path):
    from foragent.inspect_data import InspectContext, collect, inbox_payload
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    now = lambda: datetime(2026, 10, 21, tzinfo=UTC)
    tracker = FakeTracker(now=now)
    tracker.add_issue(1, "Result", labels=["lane:api"])
    store = StateStore(tmp_path / "state", now=now)
    store.start_run(1, branch="foragent/issue-1", worktree="wt", lane="api")
    store.finish_run(1, "merged")
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="Result", body="")
    tracker.merge_pr(pr, expected_head_sha=tracker.pr_head_sha(pr))
    tracker.close_issue(1)
    config = Config(language="ko", lanes={"api": {"title": "서버", "emoji": "🔧"}})
    ctx = InspectContext(tracker, store, "owner/repo", config, now)
    item = inbox_payload(collect(ctx))["items"][0]
    assert item["lane"] == "api"
    assert item["lane_display"] == "🔧 서버"
