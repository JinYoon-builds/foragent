"""The one discard guard (#1491): while a project stop is in force, worktree removal never loses work.

Git runs through the offline acceptance adapter (commits by fast-import); no real signal.
"""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent / "acceptance"))

from conftest import git_command  # noqa: E402
from foragent import orch  # noqa: E402
from test_orch_review_handoff import bind_cli, make_case  # noqa: E402
from test_one_entrance_seat import invoke  # noqa: E402

WHY = "Jin requested a stop"


@pytest.fixture
def case(git_history, tmp_path, monkeypatch):
    return make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")


def _worktree(case, name, branch=None):
    path = case.store.root / "worktrees" / name
    if branch is None:
        case.git.add_detached_worktree(path, "origin/main")
    else:
        case.git.add_worktree(path, branch, "origin/main")
    (path / "unsaved.txt").write_text("unfinished\n")
    return path


def _commits(case, branch):
    return git_command(case.root, "rev-list", "--count", branch)


def test_no_stop_removes_a_dirty_worktree_as_before(case):
    path = _worktree(case, "issue-1", "foragent/issue-1")
    before = _commits(case, "foragent/issue-1")
    case.git.remove_worktree(path)
    assert not path.exists() and _commits(case, "foragent/issue-1") == before


def test_under_a_stop_a_dirty_issue_worktree_is_saved_once_before_removal(case):
    path = _worktree(case, "issue-1", "foragent/issue-1")
    before = int(_commits(case, "foragent/issue-1"))
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert not path.exists()
    assert git_command(case.root, "show", "foragent/issue-1:unsaved.txt") == "unfinished"
    assert int(_commits(case, "foragent/issue-1")) == before + 1, "One WIP commit, not two"


def test_under_a_stop_a_clean_worktree_is_removed_without_a_commit(case):
    path = _worktree(case, "issue-1", "foragent/issue-1")
    (path / "unsaved.txt").unlink()
    before = _commits(case, "foragent/issue-1")
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert not path.exists() and _commits(case, "foragent/issue-1") == before


def test_a_detached_review_worktree_takes_its_issue_branch(case):
    path = _worktree(case, "issue-1")
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert git_command(case.root, "show", "foragent/issue-1:unsaved.txt") == "unfinished"


def test_a_dream_worktree_is_saved_on_its_dream_branch(case):
    path = _worktree(case, "dream-orch", "foragent/dream-orch")
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert git_command(case.root, "show", "foragent/dream-orch:unsaved.txt") == "unfinished"


def _kept(case):
    journal = case.store.root / "orch-events.jsonl"
    lines = journal.read_text().splitlines() if journal.exists() else []
    return [json.loads(line) for line in lines if '"worktree-kept"' in line]


def test_unsaved_work_with_no_branch_is_kept_on_disk(case):
    path = _worktree(case, "scratch")
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert (path / "unsaved.txt").read_text() == "unfinished\n"
    assert [event["unsaved"] for event in _kept(case)] == [["unsaved.txt"]]


def test_an_ignored_file_is_never_removed_unsaved(case):
    path = _worktree(case, "dream-orch", "foragent/dream-orch")
    (path / "unsaved.txt").unlink()
    memory = path / ".claude" / "agent-memory" / "orch" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("a lesson the dream wrote\n")
    (path / ".gitignore").write_text(".claude/agent-memory/\n")
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert memory.read_text() == "a lesson the dream wrote\n", "Ignored output stays until the release"
    assert any(".claude/agent-memory/" in item for event in _kept(case) for item in event["unsaved"])


def test_an_excluded_file_is_never_removed_unsaved(case):
    path = _worktree(case, "issue-1", "foragent/issue-1")
    note = path / ".foragent" / "media-orch.md"
    note.parent.mkdir()
    note.write_text("a proposal staging leaves out\n")
    orch.stop_all(case.ctx, WHY)
    case.git.remove_worktree(path)
    assert git_command(case.root, "show", "foragent/issue-1:unsaved.txt") == "unfinished", "What can be saved is"
    assert note.read_text() == "a proposal staging leaves out\n", "What cannot be committed stays on disk"
    orch.release(case.ctx)
    case.git.remove_worktree(path)  # After the release the ordinary cleanup removes it.
    assert not path.exists()


def test_release_lifts_the_guard(case):
    path = _worktree(case, "issue-1", "foragent/issue-1")
    before = _commits(case, "foragent/issue-1")
    orch.stop_all(case.ctx, WHY)
    orch.release(case.ctx)
    case.git.remove_worktree(path)
    assert not path.exists() and _commits(case, "foragent/issue-1") == before


def test_fa_orch_release_needs_no_argument_and_keeps_a_given_why(case, monkeypatch, capsys):
    bind_cli(case, monkeypatch)
    assert invoke(capsys, "orch", "release").code == 0  # Nothing is stopped: nothing changes.
    assert invoke(capsys, "orch", "stop", "--all", "--keep-work", "--why", WHY).code == 0
    result = invoke(capsys, "orch", "release")
    assert result.code == 0 and "Project stop lifted" in result.text
    from foragent import orch_stop

    assert orch_stop.active(case.store) is None
    invoke(capsys, "orch", "stop", "--all", "--keep-work", "--why", WHY)
    assert invoke(capsys, "orch", "release", "--why", "Jin is back").code == 0
    journal = (case.store.root / "orch-events.jsonl").read_text()
    assert '"kind": "release"' in journal and "Jin is back" in journal
