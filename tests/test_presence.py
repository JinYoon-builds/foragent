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


def test_a_gate_merge_counts_before_its_run_is_finished_and_only_once(tmp_path):
    from datetime import UTC, datetime, timedelta

    from foragent import presence
    from foragent.state import StateStore

    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    clock = [now - timedelta(hours=2)]
    store = StateStore(tmp_path / "state", lambda: clock[0])
    store.start_run(8, branch="foragent/issue-8", worktree=tmp_path / "w8", lane="api")
    clock[0] = now - timedelta(hours=1)
    store.record_merge(8, clock[0])
    # The gate still has GitHub calls to make before finish_run moves the merge into history.
    assert presence.counts(store, now) == (1, 0, 1)
    store.finish_run(8, outcome="merged")
    assert presence.counts(store, now) == (0, 0, 1)


def test_without_a_repo_key_a_non_github_origin_shows_zeros(project):
    (project / ".foragent.yml").write_text(yaml.safe_dump({"language": "en"}), encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text('[remote "origin"]\n\turl = https://gitlab.com/o/n.git\n',
                                            encoding="utf-8")
    assert refresh(None) == (0, "🐿 0 lanes working · ✋ 0 · 0 merged today")


def test_the_repo_key_wins_over_origin(project, monkeypatch):
    from foragent import presence
    from foragent.state import StateStore

    (project / ".git").mkdir()
    (project / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:o/origin.git\n',
                                            encoding="utf-8")
    seen = []
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo: seen.append(repo) or cls(project)))
    presence.statusline_segment(project, statusline.datetime.now(statusline.UTC))
    assert seen == ["offline/no-state"]


def test_project_without_a_repository_still_gets_its_first_minute_squirrel(project):
    """No repo key and no GitHub origin: the hook still records the start; the squirrel shows."""
    import json
    from datetime import datetime, timedelta

    from foragent import presence

    (project / ".foragent.yml").write_text(yaml.safe_dump({"language": "en"}), encoding="utf-8")
    out = io.BytesIO()
    payload = json.dumps({"session_id": "no-repo", "cwd": str(project)}).encode()
    assert statusline.session_start(io.BytesIO(payload), out) == 0
    message = json.loads(out.getvalue())["systemMessage"]
    assert message == "since the last report: 0 merges · ✋ 0 · /fa for the report"
    started = datetime.fromisoformat(json.loads(
        presence._session_path("no-repo").read_text(encoding="utf-8"))["started_at"])
    assert presence.squirrel(project, "no-repo", started + timedelta(seconds=59)) is not None
    assert presence.squirrel(project, "no-repo", started + timedelta(seconds=60)) is None
    assert presence.squirrel(project, "other-session", started) is None


STALL_BASE = "🐿 0 lanes working · ✋ 0 · 0 merged today"


@pytest.fixture
def stall_store(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from foragent.state import StateStore

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "offline/stall"}), encoding="utf-8")
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    return root, StateStore.for_repo("offline/stall", now=lambda: now), now


def _stalled_row(store, now, notes):
    import json
    from datetime import timedelta

    from foragent import orch_inbox

    row = orch_inbox.add_request(store, "planning", "opaque")
    path = store.root / "inbox" / "requests" / f"{row['id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(status="processing", created_at=(now - timedelta(minutes=10)).isoformat(), notes=notes)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_malformed_note_is_ignored_on_its_own(stall_store):
    from datetime import timedelta

    from foragent import presence

    root, store, now = stall_store
    at = (now - timedelta(hours=2)).isoformat()
    _stalled_row(store, now, ["junk", {"status": "processing"}, {"at": at},
                              {"status": "processing", "at": at}, {"at": now.isoformat()}])
    assert presence.statusline_segment(root, now) == STALL_BASE + " · Stuck requests: 1 (over 2 h) · fa orch inbox"


def test_the_request_status_decides_even_after_a_note_naming_another_status(stall_store):
    from datetime import timedelta

    from foragent import presence

    root, store, now = stall_store
    _stalled_row(store, now, [{"status": "processing", "at": (now - timedelta(hours=4)).isoformat()},
                              {"status": "done", "at": (now - timedelta(hours=3)).isoformat()}])
    assert presence.statusline_segment(root, now) == STALL_BASE + " · Stuck requests: 1 (over 4 h) · fa orch inbox"


def test_an_unreadable_requests_box_leaves_out_only_the_stall_fact(stall_store):
    from datetime import timedelta

    from foragent import presence

    root, store, now = stall_store
    _stalled_row(store, now, [{"status": "processing", "at": (now - timedelta(hours=4)).isoformat()}])
    store.start_run(7, branch="foragent/issue-7", worktree=store.worktree_path(7), lane="main")
    (store.root / "inbox" / "requests" / "r9.json").write_text("{not json", encoding="utf-8")
    assert presence.statusline_segment(root, now) == "🐿 1 lanes working · ✋ 0 · 0 merged today"
