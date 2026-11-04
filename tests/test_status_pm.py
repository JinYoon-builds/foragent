"""The PM seat authority contract (#1484): scope, entrance, refusal and session export."""

from datetime import UTC, datetime
from io import BytesIO
import json

import pytest
import yaml

from foragent import cli, status_pm, statusline
from foragent.state import StateStore

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".git/config").write_text('[remote "origin"]\nurl = git@github.com:offline/seat.git\n')
    (root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "offline/seat", "language": "en", "approvers": ["human"], "checks": "local",
        "skill_libraries": [], "lanes": {"api": {"paths": ["api/**"], "agent": "fa-api"}}}))
    monkeypatch.chdir(root)
    monkeypatch.delenv("FORAGENT_ROLE", raising=False)
    monkeypatch.delenv("FORAGENT_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
    return root, StateStore.for_repo("offline/seat")


def test_a_claim_replaces_only_the_claiming_persons_seat(project):
    root, store = project
    status_pm.claim(store, "theirs", NOW, root, who="other-person")
    status_pm.claim(store, "mine-1", NOW, root, who="me")
    result = status_pm.claim(store, "mine-2", NOW, root, who="me")
    assert result["previous"]["session_id"] == "mine-1" and not result["kept"]
    assert status_pm.read(store, "other-person")["holder"]["session_id"] == "theirs"
    assert status_pm.authority(store, "theirs", who="me") == "none"
    assert status_pm.authority(store, "mine-1", who="me") == "displaced"
    assert status_pm.authority(store, "mine-2", who="me") == "holder"


def test_a_displaced_session_that_enters_again_holds_the_seat_once_more(project):
    root, store = project
    for session in ("a", "b", "a"):
        status_pm.claim(store, session, NOW, root, who="me")
    seat = status_pm.read(store, "me")
    assert seat["holder"]["session_id"] == "a"
    assert [row["session_id"] for row in seat["displaced"]] == ["b"]
    assert status_pm.claim(store, "a", NOW, root, who="me")["kept"] is True


def test_bare_fa_without_a_claude_session_prints_one_line_and_claims_nothing(project, capsys):
    _root, store = project
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "Claude session" in out
    assert not status_pm.path(store).exists()


def test_bare_fa_names_the_session_it_replaced(project, capsys, monkeypatch):
    _root, store = project
    monkeypatch.setenv("FORAGENT_SESSION_ID", "first")
    assert cli.main([]) == 0
    monkeypatch.setenv("FORAGENT_SESSION_ID", "second")
    assert cli.main([]) == 0
    assert "first stepped back" in capsys.readouterr().out
    assert status_pm.authority(store, "first") == "displaced"


def test_a_displaced_session_refuses_flush_before_the_command_runs(project, capsys, monkeypatch):
    root, store = project
    status_pm.claim(store, "old", NOW, root)
    status_pm.claim(store, "new", NOW, root)
    ran = []
    monkeypatch.setattr(cli.commands, "module", lambda name: ran.append(name))
    monkeypatch.setenv("FORAGENT_SESSION_ID", "old")
    assert cli.main(["order", "flush", "--boundary", "now"]) == 1
    assert "PM seat moved to another terminal" in capsys.readouterr().err and ran == []
    monkeypatch.setenv("FORAGENT_SESSION_ID", "new")
    with pytest.raises(AttributeError):  # Past the seat check, the (faked) command group is reached.
        cli.main(["order", "flush", "--boundary", "now"])
    assert ran == ["order"]


def test_session_start_exports_the_session_id_for_bare_fa(project, tmp_path, monkeypatch):
    root, _store = project
    env_file = tmp_path / "claude.env"
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    payload = json.dumps({"session_id": "abc 1", "cwd": str(root)}).encode()
    statusline.session_start(BytesIO(payload), BytesIO())
    assert env_file.read_text() == "export FORAGENT_SESSION_ID='abc 1'\n"
    monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
    statusline.session_start(BytesIO(payload), BytesIO())
    assert env_file.read_text().count("export") == 1, "A role session keeps the id its spawn gave it"


def test_statusline_shows_a_displaced_session_where_the_seat_went(project, monkeypatch):
    root, store = project
    status_pm.claim(store, "first", NOW, root)
    status_pm.claim(store, "0123456789abcdef", NOW, root)
    assert statusline._pm_segment(root, "first") == "PM moved to 01234567…"
    assert statusline._pm_segment(root, "someone") == "PM 01234567…"
    notice = statusline._pm_moved_notice({"session_id": "first", "cwd": str(root)})
    assert "PM seat moved to another terminal" in notice
    assert statusline._pm_moved_notice({"session_id": "0123456789abcdef", "cwd": str(root)}) is None


def test_a_displaced_session_stays_refused_after_many_later_takeovers(project, capsys, monkeypatch):
    root, store = project
    for number in range(40):
        status_pm.claim(store, f"s{number}", NOW, root)
    assert status_pm.authority(store, "s0") == "displaced"
    monkeypatch.setenv("FORAGENT_SESSION_ID", "s0")
    assert cli.main(["order", "flush", "--boundary", "now"]) == 1
    assert "PM seat moved to another terminal" in capsys.readouterr().err
    assert "PM seat moved" in statusline._pm_moved_notice({"session_id": "s0", "cwd": str(root)})


@pytest.mark.parametrize("content", ["{not json", '{"people": []}',
                                     '{"people": {"x": {"holder": {"session_id": ""}}}}',
                                     '{"people": {"x": {"holder": {"session_id": "a"}, "displaced": [7]}}}'])
def test_an_unreadable_seat_refuses_publication_and_is_never_overwritten(project, capsys, monkeypatch, content):
    root, store = project
    store.root.mkdir(parents=True, exist_ok=True)
    status_pm.path(store).write_text(content)
    ran = []
    monkeypatch.setattr(cli.commands, "module", lambda name: ran.append(name))
    monkeypatch.setenv("FORAGENT_SESSION_ID", "old")
    assert status_pm.authority(store, "old") == "unknown"
    assert cli.main(["order", "publish", "plan.json", "--approve"]) == 1
    assert "can't be read" in capsys.readouterr().err and ran == []
    assert cli.main([]) == 1
    assert status_pm.path(store).read_text() == content, "A claim must not erase the displaced sessions"
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    assert status_pm.authority(store, None) == "none", "A caller without a session never held the seat"


def test_the_person_comes_from_the_account_not_from_changeable_env(project, monkeypatch):
    root, store = project
    status_pm.claim(store, "old", NOW, root)
    status_pm.claim(store, "new", NOW, root)
    for name in ("USER", "LOGNAME", "LNAME", "USERNAME"):
        monkeypatch.setenv(name, "somebody-else")
    assert status_pm.authority(store, "old") == "displaced"


def test_a_hook_reads_the_session_id_that_fa_claimed_with(project, monkeypatch):
    root, store = project
    status_pm.claim(store, "spawned-old", NOW, root)
    status_pm.claim(store, "new", NOW, root)
    monkeypatch.setenv("FORAGENT_SESSION_ID", "spawned-old")
    notice = statusline._pm_moved_notice({"session_id": "claude-own-id", "cwd": str(root)})
    assert notice and "PM seat moved" in notice
