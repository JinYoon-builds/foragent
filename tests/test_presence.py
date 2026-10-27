import io

import pytest
import yaml

from foragent import statusline


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(statusline.NESTED_ENV, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "offline/no-state"}), encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def refresh(previous):
    statusline._write_json(statusline.record_path(), {"enabled": True, "previous_command": previous})
    out = io.BytesIO()
    code = statusline.run(io.BytesIO(b"{}"), out)
    return code, out.getvalue().decode("utf-8")


@pytest.mark.parametrize("previous", [None, "", "fa statusline"])
def test_presence_shows_without_a_previous_statusline(project, previous):
    assert refresh(previous) == (0, "🐿 0 lanes working · ✋ 0 · 0 merged today")


def test_project_without_local_state_shows_zero_counts_after_user_output(project):
    assert refresh("printf mine") == (0, "mine  🐿 0 lanes working · ✋ 0 · 0 merged today")


def test_nested_wrapper_adds_nothing(project, monkeypatch):
    monkeypatch.setenv(statusline.NESTED_ENV, "1")
    assert refresh("printf mine") == (0, "")


def test_presence_off_with_no_previous_statusline_prints_nothing(project):
    (project / ".foragent.yml").write_text(
        yaml.safe_dump({"repo": "offline/no-state", "presence": {"statusline": False}}), encoding="utf-8")
    assert refresh(None) == (0, "")


def test_a_failed_rerun_does_not_count_the_earlier_merge_again(tmp_path):
    from datetime import UTC, datetime, timedelta

    from foragent import presence
    from foragent.state import StateStore

    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    clock = [now - timedelta(hours=3)]
    store = StateStore(tmp_path / "state", lambda: clock[0])
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "w7", lane="api")
    clock[0] = now - timedelta(hours=2)
    store.record_merge(7, clock[0])
    store.finish_run(7, outcome="merged")
    assert presence.counts(store, now)[2] == 1
    clock[0] = now - timedelta(hours=1)
    store.start_run(7, branch="foragent/issue-7", worktree=tmp_path / "w7", lane="api")
    clock[0] = now - timedelta(minutes=30)
    store.finish_run(7, outcome="checks-failed")
    assert presence.counts(store, now) == (0, 0, 1)
