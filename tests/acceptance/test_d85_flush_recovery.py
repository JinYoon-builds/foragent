"""D85 piece 4. Locked recovery checks at public filesystem/Git/tracker edges.

No private batching helpers, state filenames or journal fields are contracts.
The real Git index and ancestry are used. The existing offline adapter imports
objects and transfers local refs instead of running git commit or git push.
Each parametrized item is a separate locked interruption check.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess

import pytest

from conftest import git_command
from foragent.git import GitError
from test_d85_batched_publication import (
    BOUNDARY, DECISION, IDEA, assert_requirements, call, pending, prepare,
)
from test_order import PublishingTracker, offline_publication, plan  # noqa: F401
from test_order_preread import preread, preread_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


class Interrupted(BaseException):
    """A process stopped without running normal exception cleanup."""


@pytest.fixture
def publishing(offline_publication, git_history, monkeypatch):
    root, config, _, git, _, _ = offline_publication
    files = {
        ".foragent.yml": (root / ".foragent.yml").read_text(),
        "docs/decisions.md": "# Decisions\nExisting decision.\n",
        "docs/ideas.md": "# Ideas\nExisting idea.\n",
        "docs/rules.md": ("# Rules in force\n- Keep unrelated rules. (D95)\n"
                          "- Publish each order immediately. (D85)\n"),
        "unrelated.txt": "Original unrelated content.\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # Seed this temporary history through the offline adapter, not real commits.
    git.ensure_tracked(list(files))
    git.commit_paths(list(files), "Offline recovery baseline")
    git.push_base("main")
    git_history.tip = git.head_sha()
    git_command(root, "config", "user.name", "Offline tests")
    git_command(root, "config", "user.email", "tests@example.com")
    events = []
    commit, push = git.commit_paths, git.push_base

    def commit_paths(paths, title):
        contents = {name: (root / name).read_text() for name in paths}
        commit(paths, title)
        events.append(("commit", title, contents, git.head_sha()))

    def push_base(branch):
        push(branch)
        events.append(("push", branch))

    monkeypatch.setattr(git, "commit_paths", commit_paths)
    monkeypatch.setattr(git, "push_base", push_base)
    return root, config, PublishingTracker(events), git, events


@pytest.fixture
def recovery(preread_case, publishing, git_history):
    case = preread_case
    case.git_adapter = publishing[3]
    case.history = git_history
    # Distinguish worktree content, staged content and an untracked planning file.
    unrelated = case.root / "unrelated.txt"
    unrelated.write_text("Unrelated staged content.\n")
    git_command(case.root, "add", "--", "unrelated.txt")
    unrelated.write_text("Unrelated unstaged content.\n")
    (case.root / "notes.local.md").write_text("Untracked planning notes must survive.\n")
    case.unrelated = (unrelated.read_bytes(), git_command(case.root, "show", ":unrelated.txt"),
                      (case.root / "notes.local.md").read_bytes())
    return case


def unchanged_unrelated(case):
    assert (case.root / "unrelated.txt").read_bytes() == case.unrelated[0]
    assert git_command(case.root, "show", ":unrelated.txt") == case.unrelated[1]
    assert (case.root / "notes.local.md").read_bytes() == case.unrelated[2]
    assert "notes.local.md" not in git_command(case.root, "ls-files").splitlines()


def enqueue(case, capsys, identity, *, approve=True):
    value, path = prepare(case, capsys, identity)
    call(case, capsys, "publish", path, *(["--approve"] if approve else []), "--defer", BOUNDARY)
    return value


def append(case, capsys, kind, text):
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", kind, "--text", text)


def queue_batch(case, capsys):
    values = (enqueue(case, capsys, "first"), enqueue(case, capsys, "second", approve=False))
    append(case, capsys, "decision", DECISION)
    append(case, capsys, "idea", IDEA)
    call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "rule",
         "--text", "- Retain waiting work until publication. (D85)",
         "--replace", "- Publish each order immediately. (D85)")
    return values


def outputs(values):
    names = {"docs/decisions.md", "docs/ideas.md", "docs/rules.md"}
    for value in values:
        names.add(f"docs/specs/{value['id']}.md")
        for item in value["issues"]:
            names.update(item["acceptance_files"])
    return names


def listed_state(case, values):
    names = sorted(outputs(values))
    tree = {name: (case.root / name).read_bytes() if (case.root / name).exists() else None for name in names}
    index = git_command(case.root, "ls-files", "--stage", "--", *names)
    return tree, index


@contextmanager
def observe_restoration(case, values, baseline, monkeypatch):
    """See the listed worktree AND index restored before retry, through IO only."""
    restored, checking = [], []
    run = subprocess.run

    def observe(path=None):
        if checking:
            return
        checking.append(True)
        try:
            if listed_state(case, values) == baseline:
                restored.append(True)
        finally:
            checking.pop()

    def command(*args, **kwargs):
        result = run(*args, **kwargs)
        observe()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", command)
        with file_writes(patch, before=observe, after=observe):
            yield restored


def commits(case):
    return [event for event in case.events if event[0] == "commit"]


def finish(case, capsys, values, *, count=1):
    identities = {issue.title: issue.number for issue in case.tracker.list_issues()}
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    for value, approved in zip(values, (True, False), strict=True):
        assert_requirements(case, value, approved=approved)
    assert len(case.tracker.list_issues()) == 6
    assert identities.items() <= {issue.title: issue.number for issue in case.tracker.list_issues()}.items()
    for name, text in (("decisions", DECISION), ("ideas", IDEA),
                       ("rules", "- Retain waiting work until publication. (D85)")):
        assert (case.root / f"docs/{name}.md").read_text().count(text) == 1
    assert len(commits(case)) == count, "Recovery must never duplicate or undo a batch commit"
    staged = set(git_command(case.root, "diff", "--cached", "--name-only").splitlines())
    assert not staged.intersection(outputs(values)), "No listed batch paths may remain staged"
    unchanged_unrelated(case)
    rows = pending(case, capsys)
    assert all(row["status"] == "published" for key in ("orders", "notes") for row in rows[key])
    events = list(case.events)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert case.events == events, "An already completed batch is a no-op"


def stop(case, capsys):
    with pytest.raises(Interrupted):
        cli_exit(case.main, ["order", "flush", "--boundary", BOUNDARY])
    capsys.readouterr()
    unchanged_unrelated(case)


@contextmanager
def file_writes(monkeypatch, *, before=lambda path: None, after=lambda path: None):
    """Observe direct and atomic writes without assuming private filenames."""
    original_open, original_replace = Path.open, os.replace

    class Stream:
        def __init__(self, stream, path):
            self.stream, self.path, self.closed = stream, path, False

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def close(self):
            if not self.closed:
                self.closed = True
                self.stream.close()
                after(self.path)

        def __exit__(self, kind, value, traceback):
            result = self.stream.__exit__(kind, value, traceback)
            if not self.closed:
                self.closed = True
                if kind is None:
                    after(self.path)
            return result

    def open_path(path, mode="r", *args, **kwargs):
        writing = any(flag in mode for flag in "wax+")
        path = path.resolve()
        if writing:
            before(path)
        stream = original_open(path, mode, *args, **kwargs)
        return Stream(stream, path) if writing else stream

    def replace(source, target, *args, **kwargs):
        path = Path(target).resolve()
        before(Path(source).resolve())
        before(path)
        result = original_replace(source, target, *args, **kwargs)
        after(path)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_path)
        patch.setattr(os, "replace", replace)
        yield


def test_interruption_before_first_output_write_retains_the_batch(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)
    names = {case.root / name for name in outputs(values)}
    baseline = {path: path.read_bytes() if path.exists() else None for path in names}

    def crash(path):
        if path in names:
            raise Interrupted()

    with file_writes(monkeypatch, before=crash):
        stop(case, capsys)
    assert {path: path.read_bytes() if path.exists() else None for path in names} == baseline
    assert commits(case) == []
    finish(case, capsys, values)


@pytest.mark.parametrize("written", [
    "docs/decisions.md", "docs/ideas.md", "docs/rules.md",
    "docs/specs/2026-10-31-first.md", "docs/specs/2026-10-31-second.md",
    "tests/acceptance/test_first_api.py", "tests/acceptance/test_first_client.py",
    "tests/acceptance/test_second_api.py", "tests/acceptance/test_second_client.py",
])
def test_interruption_after_each_listed_file_restores_only_batch_paths(
    recovery, capsys, monkeypatch, written,
):
    case = recovery
    values = queue_batch(case, capsys)
    baseline = listed_state(case, values)

    def crash(path):
        if path == case.root / written:
            raise Interrupted()

    with file_writes(monkeypatch, after=crash):
        stop(case, capsys)
    assert commits(case) == []
    with observe_restoration(case, values, baseline, monkeypatch) as restored:
        finish(case, capsys, values)
    assert restored, "Restore every listed path in worktree and index before retrying the uncommitted batch"


def test_failed_commit_that_left_staged_paths_is_recoverable(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)
    baseline = listed_state(case, values)

    def fail_commit(paths, title):
        git_command(case.root, "add", "--", *paths)
        raise GitError("Injected commit interruption after staging")

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "commit_paths", fail_commit)
        message = call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    assert "interruption" in message.lower(), message
    assert outputs(values).intersection(git_command(case.root, "diff", "--cached", "--name-only").splitlines())
    unchanged_unrelated(case)
    with observe_restoration(case, values, baseline, monkeypatch) as restored:
        finish(case, capsys, values)
    assert restored, "A failed commit must leave neither batch files nor stale staged entries before retry"


def test_interruption_after_commit_before_recording_sha_never_undoes_commit(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)
    original = case.git_adapter.commit_paths

    def crash(paths, title):
        original(paths, title)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "commit_paths", crash)
        stop(case, capsys)
    sha = commits(case)[0][3]
    finish(case, capsys, values)
    assert case.git_adapter.is_ancestor(sha, "HEAD"), "The existing batch commit stays in history"


def import_later_commit(case, *, parent=None, name="later.txt"):
    """An external history change in this temporary repo; no git commit command."""
    parent = parent or case.git_adapter.head_sha()
    content = "Unrelated later change.\n"
    stream = ("commit refs/heads/main\ncommitter Offline Tests <tests@example.com> 1793443201 +0000\n"
              f"data 5\nLater\nfrom {parent}\nM 100644 inline {name}\n"
              f"data {len(content.encode())}\n{content}\n\ndone\n")
    git_command(case.root, "fast-import", "--quiet", "--force", input=stream)  # replace main without containing it
    (case.root / name).write_text(content)
    git_command(case.root, "add", "--", name)
    return case.git_adapter.head_sha()


def test_recorded_commit_is_recognized_by_ancestry_after_an_unrelated_commit(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)

    def fail_push(branch):
        raise GitError("Injected push interruption")

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", fail_push)
        call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    batch_sha = commits(case)[0][3]
    later = import_later_commit(case)
    assert later != batch_sha and case.git_adapter.is_ancestor(batch_sha, later)
    original, observed = case.git_adapter.is_ancestor, []

    def ancestry(ancestor, head):
        observed.append((ancestor, head))
        return original(ancestor, head)

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "is_ancestor", ancestry)
        finish(case, capsys, values)
    assert any(ancestor == batch_sha for ancestor, _ in observed), "Judge the recorded commit by ancestry"
    assert case.git_adapter.head_sha() == later
    assert (case.root / "later.txt").read_text() == "Unrelated later change.\n"


def test_recorded_commit_outside_head_is_retried_without_touching_unlisted_paths(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)
    before = case.git_adapter.head_sha()
    baseline = listed_state(case, values)

    def fail_push(branch):
        raise GitError("Injected push interruption")

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", fail_push)
        call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    batch_sha = commits(case)[0][3]
    git_command(case.root, "update-ref", "refs/keep/offline-batch", batch_sha)
    # Simulate an externally replaced history. Flush itself must never undo a commit.
    import_later_commit(case, parent=before)
    assert not case.git_adapter.is_ancestor(batch_sha, "HEAD")
    with observe_restoration(case, values, baseline, monkeypatch) as restored:
        finish(case, capsys, values, count=2)
    assert restored, "A non-ancestor record requires listed-path worktree and index restoration"
    assert git_command(case.root, "rev-parse", "refs/keep/offline-batch") == batch_sha
    assert (case.root / "later.txt").read_text() == "Unrelated later change.\n"


def test_interruption_after_successful_push_keeps_the_published_commit(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)
    original = case.git_adapter.push_base

    def crash(branch):
        original(branch)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", crash)
        stop(case, capsys)
    sha = commits(case)[0][3]
    finish(case, capsys, values)
    assert case.git_adapter.is_ancestor(sha, "HEAD")


@pytest.mark.parametrize("number", range(1, 7))
@pytest.mark.parametrize("side", ["before", "after"])
def test_interruption_at_each_issue_request_keeps_existing_identities(
    recovery, capsys, monkeypatch, number, side,
):
    case = recovery
    values = queue_batch(case, capsys)
    original, attempts = case.tracker.create_issue, []

    def crash(*args, **kwargs):
        attempts.append(args)
        if len(attempts) == number and side == "before":
            raise Interrupted()
        result = original(*args, **kwargs)
        if len(attempts) == number and side == "after":
            raise Interrupted()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(case.tracker, "create_issue", crash)
        stop(case, capsys)
    finish(case, capsys, values)


@pytest.mark.parametrize("operation", ["update_issue", "add_sub_issue", "add_blocked_by", "add_labels"])
def test_interruption_after_issue_metadata_write_is_idempotent(recovery, capsys, monkeypatch, operation):
    case = recovery
    values = queue_batch(case, capsys)
    original = getattr(case.tracker, operation)

    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(case.tracker, operation, crash)
        stop(case, capsys)
    finish(case, capsys, values)


def test_interruption_before_final_local_checkpoint_repeats_no_effects(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)

    def crash(path):
        issues = case.tracker.list_issues()
        approved = [issue for issue in issues if "approved" in issue.labels]
        local = path.is_relative_to(Path.home()) or path.is_relative_to(case.root / ".foragent")
        if local and len(issues) == 6 and len(approved) == 2:
            raise Interrupted()

    with file_writes(monkeypatch, before=crash):
        stop(case, capsys)
    finish(case, capsys, values)


def test_two_failed_pushes_keep_one_pending_batch_and_all_later_work(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)
    later = "A note appended while the first batch waits for its push."

    def fail_push(branch):
        raise GitError("Injected repeated push interruption")

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", fail_push)
        call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
        batch_sha = commits(case)[0][3]
        append(case, capsys, "decision", later)
        third = enqueue(case, capsys, "later")
        call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    assert len(commits(case)) == 1 and case.git_adapter.head_sha() == batch_sha
    assert later not in (case.root / "docs/decisions.md").read_text()
    rows = pending(case, capsys)
    assert any(row["text"] == later and row["status"] == "waiting" for row in rows["notes"])
    assert any(row["id"] == third["id"] and row["status"] == "waiting" for row in rows["orders"])
    unchanged_unrelated(case)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    for value, approved in (*zip(values, (True, False)), (third, True)):
        assert_requirements(case, value, approved=approved)
    assert len(case.tracker.list_issues()) == 9
    assert (case.root / "docs/decisions.md").read_text().count(later) == 1
    assert len(commits(case)) == 2 and case.git_adapter.is_ancestor(batch_sha, "HEAD")
    unchanged_unrelated(case)


def test_pending_batch_rebases_onto_a_moved_base_before_new_work(recovery, capsys, monkeypatch):
    case = recovery
    values = queue_batch(case, capsys)

    def fail_push(branch):
        raise GitError("Injected push interruption before base movement")

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", fail_push)
        call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    case.history.update({"upstream.txt": "A moved upstream base.\n"})
    moved = case.history.tip
    finish(case, capsys, values)
    assert case.git_adapter.is_ancestor(moved, "HEAD")
    assert case.git_adapter.is_ancestor(moved, "origin/main")
    assert (case.root / "upstream.txt").read_text() == "A moved upstream base.\n"


def snapshot(case):
    return {path: path.read_bytes() for root in (case.root, Path.home()) for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts}


def test_flush_on_another_branch_refuses_in_one_line_without_changes(recovery, capsys):
    case = recovery
    queue_batch(case, capsys)
    git_command(case.root, "update-ref", "refs/heads/other", case.git_adapter.head_sha())
    git_command(case.root, "symbolic-ref", "HEAD", "refs/heads/other")
    before, events = snapshot(case), list(case.events)
    index = git_command(case.root, "ls-files", "--stage")
    message = call(case, capsys, "flush", "--boundary", BOUNDARY, ok=False)
    assert len(message.strip().splitlines()) == 1 and "main" in message, message
    assert snapshot(case) == before and case.events == events
    assert git_command(case.root, "ls-files", "--stage") == index
    unchanged_unrelated(case)


@pytest.mark.parametrize("kind", ["decision", "idea", "rule", "order", "order-revision"])
def test_appended_work_between_read_and_checkpoint_survives_for_next_flush(
    recovery, capsys, monkeypatch, kind,
):
    case = recovery
    enqueue(case, capsys, "original")
    first = "The first planning decision."
    append(case, capsys, "decision", first)
    stored = {path: data for path, data in snapshot(case).items() if first.encode() in data}
    assert stored
    late = "A concurrent note must survive the flush checkpoint."
    late_order = []
    original = case.git_adapter.push_base
    injected = []

    def during_push(branch):
        original(branch)
        if not injected:
            injected.append(True)
            if kind == "order":
                late_order.append(enqueue(case, capsys, "concurrent", approve=False))
            elif kind == "order-revision":
                value, path = prepare(case, capsys, "original", clean=False)
                value["spec_markdown"] += "Keep the concurrent revision for the next boundary.\n"
                for item in value["issues"]:
                    item["goal"] += " Keep this newer revision."
                    for name in item["acceptance_files"]:
                        item["acceptance_files"][name] += "\n# This is the concurrent locked-test revision.\n"
                path.write_text(json.dumps(value))
                preread(case, capsys, *("NONE" for _ in value["issues"]))
                call(case, capsys, "publish", path, "--defer", BOUNDARY)
                late_order.append(value)
            elif kind == "rule":
                call(case, capsys, "note", "--defer", BOUNDARY, "--kind", "rule", "--text", late,
                     "--replace", "- Publish each order immediately. (D85)")
            else:
                append(case, capsys, kind, late)

    with monkeypatch.context() as patch:
        patch.setattr(case.git_adapter, "push_base", during_push)
        call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert injected
    for path, before in stored.items():
        assert path.is_file() and path.read_bytes().startswith(before), "Flush must never rewrite the waiting log"
    rows = pending(case, capsys)
    if kind in ("order", "order-revision"):
        assert any(row["id"] == late_order[0]["id"] and row["status"] == "waiting" for row in rows["orders"])
    else:
        assert any(row["text"] == late and row["status"] == "waiting" for row in rows["notes"])
        destination = {"decision": "decisions", "idea": "ideas", "rule": "rules"}[kind]
        assert late not in (case.root / f"docs/{destination}.md").read_text()
    assert len(commits(case)) == 1
    identities = {issue.title: issue.number for issue in case.tracker.list_issues()}
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    if kind in ("order", "order-revision"):
        assert_requirements(case, late_order[0], approved=False)
        if kind == "order-revision":
            assert len(case.tracker.list_issues()) == 3
            assert identities == {issue.title: issue.number for issue in case.tracker.list_issues()}
    else:
        assert (case.root / f"docs/{destination}.md").read_text().count(late) == 1
    assert (case.root / "docs/decisions.md").read_text().count(first) == 1
    for path, before in stored.items():
        assert path.is_file() and path.read_bytes().startswith(before)
    unchanged_unrelated(case)


def test_preexisting_files_with_observed_temporary_names_survive_flush(recovery, capsys, monkeypatch):
    case = recovery
    append(case, capsys, "decision", "Probe the publication's temporary destinations.")
    touched = set()
    with file_writes(monkeypatch, before=touched.add, after=touched.add):
        call(case, capsys, "flush", "--boundary", BOUNDARY)
    append(case, capsys, "idea", "Publish a second note while preexisting files are present.")
    # Discover names from observable IO. Fresh random names need no fixed-name contract.
    candidates = {path for path in touched if path.is_relative_to(case.root) and not path.exists()
                  and ".git" not in path.relative_to(case.root).parts}
    candidates.add(case.root / "docs/notes.md")
    sentinel = b"A preexisting file belongs to another writer.\n"
    for path in candidates:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sentinel)
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert all(path.is_file() and path.read_bytes() == sentinel for path in candidates), (
        "Flush may delete only temporary files it created during this flush", candidates)
    unchanged_unrelated(case)


def test_empty_flush_is_a_noop_for_worktree_index_and_untracked_notes(recovery, capsys):
    case = recovery
    before, events = snapshot(case), list(case.events)
    index = git_command(case.root, "ls-files", "--stage")
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert case.events == events
    assert git_command(case.root, "ls-files", "--stage") == index
    # Creating local control state is allowed; every preexisting byte must survive.
    assert before.items() <= snapshot(case).items()
    unchanged_unrelated(case)


def test_orders_adding_lanes_publish_immediately_and_never_join_batch_recovery(recovery, capsys):
    from foragent.config import load_config

    case = recovery
    ordinary = enqueue(case, capsys, "ordinary")
    value, path = prepare(case, capsys, "lane-order", clean=False)
    value["new_lanes"] = [{"name": "web", "paths": ["web/**"], "skills": []}]
    path.write_text(json.dumps(value))
    # Keep today's unapproved lane-creation path; approval still needs its own preread.
    call(case, capsys, "publish", path, "--defer", BOUNDARY)
    assert_requirements(case, value, approved=False)
    assert "web" in load_config(case.root).lanes
    assert not any(row["id"] == value["id"] and row["status"] == "waiting"
                   for row in pending(case, capsys)["orders"])
    lane_paths = {name: (case.root / name).read_bytes()
                  for name in (".foragent.yml", ".claude/agents/fa-web.md", ".gitattributes")}
    before = len(commits(case))
    call(case, capsys, "flush", "--boundary", BOUNDARY)
    assert_requirements(case, ordinary, approved=True)
    assert {name: (case.root / name).read_bytes() for name in lane_paths} == lane_paths
    assert all(not set(event[2]).intersection(lane_paths) for event in commits(case)[before:])
    unchanged_unrelated(case)
