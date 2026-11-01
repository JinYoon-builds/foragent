"""Recovery cases around the plumbing rebuild, in addition to locked edge checks."""

import pytest

from conftest import git_command
from foragent import order_flush, order_flush_journal as journal, order_notes
from foragent.git import GitError
from foragent.order import PlanError
from foragent.state import StateStore
from test_order import offline_publication  # noqa: F401


class Interrupted(BaseException):
    pass


@pytest.fixture
def waiting(offline_publication, git_history):
    root, config, tracker, git, _calls, _origin = offline_publication
    for name, content in {"docs/decisions.md": "# Decisions\n", "unrelated.txt": "Original\n"}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git.ensure_tracked(["docs/decisions.md", "unrelated.txt"])
    git.commit_paths(["docs/decisions.md", "unrelated.txt"], "Baseline")
    git.push_base("main")
    git_history.tip = git.head_sha()
    (root / "unrelated.txt").write_text("Staged\n")
    git_command(root, "add", "--", "unrelated.txt")
    (root / "unrelated.txt").write_text("Unstaged\n")
    store = StateStore.for_repo("owner/repo")
    order_notes.append(store, kind="decision", boundary="chosen", content="One retained decision")
    return root, config, tracker, git, store


def flush(case, boundary="chosen"):
    root, config, tracker, git, store = case
    return order_flush.flush(root, config, tracker, git, store, repo="owner/repo", boundary=boundary)


def fail_push(case, monkeypatch):
    def fail(branch):
        raise GitError("push interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(case[3], "push_base", fail)
        with pytest.raises(GitError, match="push interrupted"):
            flush(case)


def intact(case):
    root, _config, _tracker, git, store = case
    assert (root / "unrelated.txt").read_text() == "Unstaged\n"
    assert git_command(root, "show", ":unrelated.txt") == "Staged"
    assert (root / "docs/decisions.md").read_text().count("One retained decision") == 1
    assert not git.uncommitted_paths(["docs/decisions.md"])
    assert order_notes.pending(store)["notes"][0]["status"] == "published"
    assert flush(case)["status"] == "empty"


@pytest.mark.parametrize("edge", ["before-tree", "after-tree", "after-ref", "before-record"])
def test_rebuild_interruption_finishes_tree_ref_and_journal_once(waiting, git_history, monkeypatch, edge):
    case = waiting
    root, _config, _tracker, git, store = case
    fail_push(case, monkeypatch)
    old = git.head_sha()
    git_history.update({"upstream.txt": "Moved base\n"})
    run, append = git._call, journal.append

    def command(args):
        if args[0] == "read-tree" and edge == "before-tree":
            raise Interrupted()
        result = run(args)
        if (args[0] == "read-tree" and edge == "after-tree"
                or args[0] == "update-ref" and edge == "after-ref"):
            raise Interrupted()
        return result

    def record(store, kind, batch, **values):
        if kind == "rebased" and edge == "before-record":
            raise Interrupted()
        append(store, kind, batch, **values)

    with monkeypatch.context() as patch:
        patch.setattr(git, "_call", command)
        patch.setattr(journal, "append", record)
        with pytest.raises(Interrupted):
            flush(case)
    intent = next(row for row in journal.entries(store) if row["kind"] == "rebuilding")
    flush(case)
    assert git.head_sha() == intent["sha"] != old
    assert git.is_ancestor(git_history.tip, "HEAD")
    # The offline push transfers the bare ref; it doesn't update tracking refs.
    assert git_command(git_history.origin, "rev-parse", "refs/heads/main") == git.head_sha()
    assert (root / "upstream.txt").read_text() == "Moved base\n"
    assert len(git_command(root, "log", "--format=%H", "--grep=Foragent batch").splitlines()) == 1
    intact(case)


def test_moved_base_with_local_descendants_refuses_and_retains_pending(waiting, git_history, monkeypatch):
    root, _config, _tracker, git, store = waiting
    fail_push(waiting, monkeypatch)
    (root / "later.txt").write_text("Later\n")
    git.ensure_tracked(["later.txt"])
    git.commit_paths(["later.txt"], "Unrelated descendant")
    sha = git.head_sha()
    git_history.update({"upstream.txt": "Moved base\n"})
    index = git_command(root, "ls-files", "--stage")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    with pytest.raises(PlanError, match="local descendants") as error:
        flush(waiting)
    assert len(str(error.value).splitlines()) == 1
    assert git.head_sha() == sha
    assert git_command(root, "ls-files", "--stage") == index
    assert all(path.read_bytes() == data for path, data in before.items())
    assert order_notes.pending(store)["notes"][0]["status"] == "waiting"
    assert not any(row["kind"] == "rebuilding" for row in journal.entries(store))


def test_pending_other_boundary_finishes_before_selecting_new_work(waiting, monkeypatch):
    root, _config, _tracker, _git, store = waiting
    fail_push(waiting, monkeypatch)
    order_notes.append(store, kind="idea", boundary="later", content="A later idea")
    assert flush(waiting, "later")["status"] == "published"
    assert (root / "docs/ideas.md").read_text() == "- A later idea\n"
    rows = journal.entries(store)
    selected = [row for row in rows if row["kind"] == "selected"]
    assert len(selected) == 2
    first_done = next(i for i, row in enumerate(rows) if row["kind"] == "done")
    assert rows.index(selected[1]) > first_done
    intact(waiting)


def test_empty_path_list_never_stages_all_unrelated_changes(waiting, monkeypatch):
    root, config, tracker, git, store = waiting
    batch = {"kind": "selected", "batch": "empty", "boundary": "elsewhere",
             "orders": [], "notes": [], "paths": []}
    journal.append(store, **batch)
    before = git_command(root, "ls-files", "--stage")
    # An empty intent is a Git no-op, even while an unrelated note waits.
    def forbidden(*args):
        pytest.fail("empty path list must not stage or commit")

    monkeypatch.setattr(git, "ensure_tracked", forbidden)
    monkeypatch.setattr(git, "commit_paths", forbidden)
    result = order_flush.flush(root, config, tracker, git, store, repo="owner/repo", boundary="elsewhere")
    assert result["status"] == "published"
    assert git_command(root, "ls-files", "--stage") == before
    assert not (root / "docs/decisions.md").read_text().count("One retained decision")
