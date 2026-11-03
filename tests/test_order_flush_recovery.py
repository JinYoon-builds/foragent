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


def interrupt_rebuild(case, git_history, monkeypatch, edge):
    git = case[3]
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
        if kind == "rebased" and edge == "after-record":
            raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(git, "_call", command)
        patch.setattr(journal, "append", record)
        with pytest.raises(Interrupted):
            flush(case)
    return old


@pytest.mark.parametrize("edge", ["before-tree", "after-tree", "after-ref", "before-record"])
def test_rebuild_interruption_finishes_tree_ref_and_journal_once(waiting, git_history, monkeypatch, edge):
    case = waiting
    root, _config, _tracker, git, store = case
    old = interrupt_rebuild(case, git_history, monkeypatch, edge)
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


def test_landed_batch_is_not_rechecked_against_the_commit_it_made(waiting, monkeypatch):
    """A statement order's evidence names the base it lands on; its own commit moves HEAD."""
    from foragent import order

    root, config, tracker, git, store = waiting
    plan = {"id": "2026-11-01-reviewed", "title": "Reviewed", "summary": "Reviewed", "spec_markdown": "# Reviewed\n",
            "new_lanes": [], "edits": [], "issues": [
                {"key": "api", "title": "API", "goal": "Do it.", "out_of_scope": "Other.", "done_when": ["true"],
                 "checklist": [], "lane": "main", "size": "s", "blocked_by": [], "acceptance_files": {},
                 "statements": ["It works."]}]}
    from foragent import order_submissions
    monkeypatch.setattr(order, "require_clean_preread", lambda *args, **kwargs: None)
    order_submissions.retain(plan, "owner/repo", "chosen", approve=True, approve_keys=[], approve_numbers=[],
                             language=config.language, files={"docs/specs/2026-11-01-reviewed.md": "# Reviewed\n"})
    base = git.head_sha()
    monkeypatch.setattr(order, "require_reviewed_tests",
                        lambda *args, **kwargs: {"api": {"base_revision": base}})
    monkeypatch.setattr(order.order_reviewed, "require_reviewed_bytes", lambda *args, **kwargs: None)
    monkeypatch.setattr(order.order_reviewed, "require_route_kept", lambda *args, **kwargs: None)
    fail_push(waiting, monkeypatch)
    assert git.head_sha() != base
    assert flush(waiting)["status"] == "published"
    assert [issue.title for issue in tracker.list_issues() if issue.title == "API"] == ["API"]
    assert git_command(root, "log", "--format=%s", "--grep=Foragent batch").count("Foragent batch") == 1


def approve_owner(case):
    """An approved issue outside the batch comes to lock the file the batch rewrites."""
    tracker = case[2]
    number = tracker.create_issue("Owner", "No locks yet.", ["approved"])
    tracker.update_issue(number, title="Owner", body=('<!-- foragent:plan id=other map={} acceptance={"%d": '
                                       '{"docs/decisions.md": "locked"}} -->' % number))
    return number


def test_retried_push_refuses_an_owner_approved_since_the_commit(waiting, monkeypatch):
    _root, _config, tracker, git, store = waiting
    fail_push(waiting, monkeypatch)
    sha = git.head_sha()
    owner = approve_owner(waiting)
    pushed = []
    with monkeypatch.context() as patch:
        patch.setattr(git, "push_base", pushed.append)
        with pytest.raises(PlanError, match=f"#{owner}"):
            flush(waiting)
    assert not pushed
    assert git.head_sha() == sha
    assert order_notes.pending(store)["notes"][0]["status"] == "waiting"
    tracker.remove_labels(owner, ["approved"])
    assert flush(waiting)["status"] == "published"
    intact(waiting)


def test_uncommitted_resume_refuses_an_owner_approved_since_the_intent(waiting, monkeypatch):
    _root, _config, tracker, git, _store = waiting
    head = git.head_sha()

    def stop(paths, title):
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(git, "commit_paths", stop)
        with pytest.raises(Interrupted):
            flush(waiting)
    owner = approve_owner(waiting)
    with pytest.raises(PlanError, match=f"#{owner}"):
        flush(waiting)
    assert git.head_sha() == head
    tracker.remove_labels(owner, ["approved"])
    assert flush(waiting)["status"] == "published"
    intact(waiting)


def test_resume_holds_the_shared_publication_gate(waiting, monkeypatch):
    from contextlib import contextmanager

    from foragent import order_reviewed

    git = waiting[3]
    fail_push(waiting, monkeypatch)
    held, gate, push = [], order_reviewed.publication_gate, git.push_base

    @contextmanager
    def spy(repo):
        with gate(repo):
            held.append(True)
            yield
            held.pop()

    def pushed(branch):
        assert held
        return push(branch)

    monkeypatch.setattr(order_reviewed, "publication_gate", spy)
    monkeypatch.setattr(git, "push_base", pushed)
    assert flush(waiting)["status"] == "published"
    assert not held


def interrupt_before_recording(case, monkeypatch):
    git, append = case[3], journal.append

    def record(store, kind, batch, **values):
        if kind == "committed":
            raise Interrupted()
        append(store, kind, batch, **values)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "append", record)
        with pytest.raises(Interrupted):
            flush(case)
    return git.head_sha()


def stop_recorded(case, monkeypatch):
    fail_push(case, monkeypatch)
    return case[3].head_sha()


def move_back(case, sha):
    """This clone's branch lost the published batch commit, keeping unrelated bytes."""
    root = case[0]
    parent = git_command(root, "rev-parse", f"{sha}~1")
    git_command(root, "read-tree", "-m", "-u", sha, parent)
    git_command(root, "update-ref", "refs/heads/main", parent, sha)


def forbid_commit(case, monkeypatch):
    # The fixture's fixed clock gives an identical retry the same SHA, hiding the repeat.
    def forbidden(paths, title):
        pytest.fail("a published batch must not be committed again")

    monkeypatch.setattr(case[3], "commit_paths", forbidden)


def published_once(case, origin):
    root = case[0]
    assert (root / "docs/decisions.md").read_text().count("One retained decision") == 1
    assert git_command(origin, "show", "refs/heads/main:docs/decisions.md").count("One retained decision") == 1
    assert len(git_command(origin, "log", "--format=%H", "--grep=Foragent batch", "refs/heads/main").splitlines()) == 1
    intact(case)


@pytest.mark.parametrize("stop", [interrupt_before_recording, stop_recorded], ids=["unrecorded", "recorded"])
@pytest.mark.parametrize("behind", [False, True], ids=["in-head", "outside-head"])
def test_commit_pushed_externally_is_recognized_not_published_again(waiting, git_history, monkeypatch, stop, behind):
    sha = stop(waiting, monkeypatch)
    waiting[3].push_base("main")  # Someone else's push of this clone's branch.
    if behind:
        move_back(waiting, sha)
    forbid_commit(waiting, monkeypatch)
    assert flush(waiting)["status"] == "published"
    assert waiting[3].head_sha() == sha
    published_once(waiting, git_history.origin)


def rewritten_copy(case, git_history, sha):
    """Publish the same batch commit, under its title, rewritten onto a moved base."""
    root = case[0]
    git_history.update({"upstream.txt": "Moved base\n"})
    git_command(root, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
    tree = git_command(root, "merge-tree", "--write-tree", "origin/main", sha).splitlines()[0]
    subject = git_command(root, "log", "-1", "--format=%s", sha)
    copy = git_command(root, "-c", "user.name=Other", "-c", "user.email=other@example.com",
                       "commit-tree", tree, "-p", "origin/main", "-m", subject)
    git_command(root, "update-ref", "refs/copy/batch", copy)
    git_command(git_history.origin, "fetch", str(root), "refs/copy/batch:refs/heads/main")
    return copy


@pytest.mark.parametrize("stop", [interrupt_before_recording, stop_recorded], ids=["unrecorded", "recorded"])
def test_rewritten_copy_on_the_remote_replaces_the_local_commit_without_a_rebuild(
    waiting, git_history, monkeypatch, stop,
):
    copy = rewritten_copy(waiting, git_history, stop(waiting, monkeypatch))
    assert flush(waiting)["status"] == "published"
    assert waiting[3].head_sha() == copy
    assert git_command(git_history.origin, "rev-parse", "refs/heads/main") == copy
    published_once(waiting, git_history.origin)


def test_rewritten_copy_with_local_descendants_refuses_and_retains_pending(waiting, git_history, monkeypatch):
    root, _config, _tracker, git, store = waiting
    sha = stop_recorded(waiting, monkeypatch)
    copy = rewritten_copy(waiting, git_history, sha)
    (root / "later.txt").write_text("Later\n")
    git.ensure_tracked(["later.txt"])
    git.commit_paths(["later.txt"], "Unrelated descendant")
    later = git.head_sha()
    with pytest.raises(PlanError, match="local descendants") as error:
        flush(waiting)
    assert len(str(error.value).splitlines()) == 1
    assert git.head_sha() == later != copy
    assert git_command(git_history.origin, "rev-parse", "refs/heads/main") == copy
    assert order_notes.pending(store)["notes"][0]["status"] == "waiting"


def test_published_batch_clears_leftover_batch_bytes_before_catching_up(waiting, git_history, monkeypatch):
    root, _config, _tracker, git, _store = waiting
    sha = stop_recorded(waiting, monkeypatch)
    git.push_base("main")
    move_back(waiting, sha)
    (root / "docs/decisions.md").write_text("A partial batch write\n")
    git_command(root, "add", "--", "docs/decisions.md")
    forbid_commit(waiting, monkeypatch)
    assert flush(waiting)["status"] == "published"
    assert git.head_sha() == sha
    published_once(waiting, git_history.origin)


def merge_externally(case, git_history, sha):
    """Someone merges the recorded batch commit into the moved remote base."""
    root = case[0]
    git_command(root, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
    tree = git_command(root, "merge-tree", "--write-tree", "origin/main", sha).splitlines()[0]
    merged = git_command(root, "-c", "user.name=Other", "-c", "user.email=other@example.com",
                         "commit-tree", tree, "-p", "origin/main", "-p", sha, "-m", "Merge the batch")
    git_command(root, "update-ref", "refs/copy/batch", merged)
    git_command(git_history.origin, "fetch", str(root), "refs/copy/batch:refs/heads/main")
    return merged


def forbid_rebuild(case, monkeypatch):
    git, run = case[3], case[3]._call

    def command(args):
        assert args[0] != "commit-tree", "a published batch must not be rebuilt"
        return run(args)

    monkeypatch.setattr(git, "_call", command)
    forbid_commit(case, monkeypatch)


def assert_only_recorded_commit_published(case, git_history, sha, merged):
    root, _config, _tracker, git, store = case
    assert git.head_sha() == sha
    assert git_command(git_history.origin, "rev-parse", "refs/heads/main") == merged
    assert not git_command(root, "diff", "--name-only", "HEAD", "--", "upstream.txt", "docs/decisions.md")
    assert not (root / "upstream.txt").exists()
    assert not git.uncommitted_paths(["docs/decisions.md"])
    assert [row["sha"] for row in journal.entries(store) if row["kind"] == "pushed"] == [sha]
    published_once(case, git_history.origin)


@pytest.mark.parametrize("edge", ["before-tree", "after-tree", "after-ref", "before-record", "after-record"])
def test_interrupted_rebuild_stops_when_the_batch_was_merged_externally(waiting, git_history, monkeypatch, edge):
    sha = interrupt_rebuild(waiting, git_history, monkeypatch, edge)
    merged = merge_externally(waiting, git_history, sha)
    forbid_rebuild(waiting, monkeypatch)
    assert flush(waiting)["status"] == "published"
    assert_only_recorded_commit_published(waiting, git_history, sha, merged)


def test_retried_push_does_not_rebuild_a_batch_merged_externally(waiting, git_history, monkeypatch):
    sha = stop_recorded(waiting, monkeypatch)
    git_history.update({"upstream.txt": "Moved base\n"})
    merged = merge_externally(waiting, git_history, sha)
    forbid_rebuild(waiting, monkeypatch)
    assert flush(waiting)["status"] == "published"
    assert_only_recorded_commit_published(waiting, git_history, sha, merged)
