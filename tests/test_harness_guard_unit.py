"""The committed-diff test-harness guard (#466, scope A)."""

from datetime import UTC, datetime
import subprocess
from types import SimpleNamespace

import pytest

from conftest import git_command
from foragent.config import Config
from foragent.git import GitRepo
from foragent.harness_guard import guard_measured, listed
from foragent.reasons import Reason
from foragent.state import StateStore
from foragent.tracker import FakeTracker

NOW = datetime(2026, 10, 30, tzinfo=UTC)
PYPROJECT = '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'


def raw_git(repo, *args, stdin=b""):
    return subprocess.run(["git", *args], cwd=repo, input=stdin, capture_output=True, check=True).stdout


def commit(repo, entries, parent=None):
    """Commit the index plus raw (mode, path bytes, content or sha) entries; no worktree files needed."""
    raw_git(repo, "read-tree", *([parent] if parent else ["--empty"]))  # start from the parent's tree
    lines = b""
    for mode, path, content in entries:
        if content is None:
            lines += b"0 " + b"0" * 40 + b"\t" + path + b"\0"
            continue
        sha = content if mode == b"160000" else raw_git(repo, "hash-object", "-w", "--stdin", stdin=content).strip()
        lines += mode + b" " + sha + b"\t" + path + b"\0"
    if lines:
        raw_git(repo, "update-index", "-z", "--index-info", stdin=lines)
    tree = git_command(repo, "write-tree")
    return git_command(repo, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", "c")


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_command(repo, "init", "-q", "--initial-branch=main")
    git_command(repo, "config", "user.email", "t@example.com")
    git_command(repo, "config", "user.name", "T")
    base = commit(repo, [(b"100644", b"pyproject.toml", PYPROJECT.encode()),
                         (b"100644", b"tests/conftest.py", b"# base\n")])
    git_command(repo, "update-ref", "refs/heads/target", base)
    git_command(repo, "update-ref", "refs/heads/main", base)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    store.start_run(1, branch="foragent/issue-1", worktree=repo)
    ctx = SimpleNamespace(store=store, config=Config(), now=lambda: NOW, repo="owner/repo")

    def head(entries, files=(), *, tree=False):
        sha = commit(repo, entries, parent=base)
        git_command(repo, "update-ref", "refs/heads/main", sha)
        measured = git_command(repo, "rev-parse", f"{sha}^{{tree}}") if tree else sha
        return guard_measured(ctx, 1, GitRepo(repo), "target", measured, list(files), tree=tree)

    return SimpleNamespace(repo=repo, base=base, store=store, ctx=ctx, head=head)


@pytest.mark.parametrize("name", ["tests/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                                  "pkg/sitecustomize.py", "pkg/usercustomize.py"])
def test_unlisted_committed_harness_change_holds_and_a_listed_one_proceeds(case, name):
    entries = [(b"100644", name.encode(), b"# changed\n")]
    assert case.head(entries) == [name]
    assert case.head(entries, [name]) == []
    assert case.head(entries, ["**"]) == []


def test_staged_tree_is_guarded_like_a_commit(case):
    assert case.head([(b"100644", b"tests/conftest.py", b"# staged\n")], tree=True) == ["tests/conftest.py"]


def test_deletion_and_addition_count(case):
    assert case.head([(b"100644", b"tests/conftest.py", None)]) == ["tests/conftest.py"]
    assert case.head([(b"100644", b"new/conftest.py", b"x\n")]) == ["new/conftest.py"]


def test_mode_change_holds_unless_listed_and_pyproject_mode_alone_proceeds(case):
    entries = [(b"100755", b"tests/conftest.py", b"# base\n")]
    assert case.head(entries) == ["tests/conftest.py"]
    assert case.head(entries, ["tests/conftest.py"]) == []
    assert case.head([(b"100755", b"pyproject.toml", PYPROJECT.encode())]) == []


@pytest.mark.parametrize("text,held", [
    (PYPROJECT.replace('"-q"', '"-x"'), True),
    (PYPROJECT.replace('"x"', '"y"'), False),
    (PYPROJECT + '\n[tool.pytest.ini_options.markers]\nslow = "s"\n', True),
    (PYPROJECT + '\n[project.entry-points.pytest11]\nplug = "plug"\n', True),
    (PYPROJECT.replace('addopts = "-q"', "addopts = 0"), True),
])
def test_pyproject_counts_only_its_pytest_tables(case, text, held):
    assert case.head([(b"100644", b"pyproject.toml", text.encode())]) == (["pyproject.toml"] if held else [])


@pytest.mark.parametrize("entries,path", [
    ([(b"120000", b"tests/conftest.py", b"elsewhere.py")], "tests/conftest.py"),
    ([(b"160000", b"tests/conftest.py", b"a" * 40)], "tests/conftest.py"),
    ([(b"100644", b"pyproject.toml", b"[broken\n")], "pyproject.toml"),
    ([(b"100644", b"pkg/conftest.py/inside.txt", b"x\n")], "pkg/conftest.py/inside.txt"),
    ([(b"100644", b"caf\xff/conftest.py", b"x\n")], '"caf\\377/conftest.py"'),
], ids=["symlink", "submodule", "unparsable-pyproject", "harness-named-directory", "non-utf8-path"])
def test_entries_that_cannot_be_compared_exactly_always_hold_even_listed(case, entries, path):
    assert case.head(entries, ["**"]) == [path]


def test_symlink_on_the_base_side_holds(case):
    symlink_base = commit(case.repo, [(b"120000", b"conftest.py", b"tests/conftest.py")], parent=case.base)
    git_command(case.repo, "update-ref", "refs/heads/target", symlink_base)
    head = commit(case.repo, [(b"100644", b"conftest.py", b"# now a file\n")], parent=symlink_base)
    assert guard_measured(case.ctx, 1, GitRepo(case.repo), "target", head, ["**"]) == ["conftest.py"]


def test_harness_changes_already_on_the_target_do_not_count(case):
    target = commit(case.repo, [(b"100644", b"tests/conftest.py", b"# target\n")], parent=case.base)
    git_command(case.repo, "update-ref", "refs/heads/target", target)
    head = commit(case.repo, [(b"100644", b"feature.txt", b"done\n")], parent=case.base)
    assert guard_measured(case.ctx, 1, GitRepo(case.repo), "target", head, []) == []


def test_files_entries_match_raw_bytes_without_normalizing(case):
    entries = [(b"100644", b"tests/conftest.py", b"# changed\n")]
    for entry in ("/tests/conftest.py", "./tests/conftest.py", "tests//conftest.py", "tests/conftest.py/"):
        assert case.head(entries, [entry]) == ["tests/conftest.py"]
    assert listed(b"caf\xc3\xa9/conftest.py", ["café/conftest.py"])
    assert not listed(b"cafe\xcc\x81/conftest.py", ["café/conftest.py"])  # NFD path, NFC entry
    assert listed(b"a/" * 3000 + b"conftest.py", ["**/conftest.py"])


def test_a_passed_pair_is_recorded_and_not_judged_again(case, monkeypatch):
    from foragent import harness_guard

    assert case.head([(b"100644", b"feature.txt", b"done\n")], tree=True) == []
    record = case.store.get_run(1).harness_guard
    assert record["merge_base"] == case.base and len(record["guarded"]) == 40
    tree = record["guarded"]
    calls = []
    monkeypatch.setattr(harness_guard, "_holds", lambda *args: calls.append(args) or True)
    # The commit whose tree was guarded counts as guarded: nothing is judged again.
    head = git_command(case.repo, "commit-tree", tree, "-p", case.base, "-m", "same tree")
    assert guard_measured(case.ctx, 1, GitRepo(case.repo), "target", head, []) == []
    assert calls == []
    # A new merge base runs the guard again.
    moved = commit(case.repo, [(b"100644", b"other.txt", b"x\n")], parent=case.base)
    git_command(case.repo, "update-ref", "refs/heads/target", moved)
    merged = git_command(case.repo, "commit-tree", tree, "-p", head, "-p", moved, "-m", "merge")
    assert guard_measured(case.ctx, 1, GitRepo(case.repo), "target", merged, []) != []
    assert calls


def test_a_diff_that_cannot_be_read_holds(case):
    held = guard_measured(case.ctx, 1, GitRepo(case.repo), "target", "1" * 40, [])
    assert len(held) == 1 and held[0].startswith("(the change could not be read:")


def test_flag_needs_human_blocks_and_hands_over_with_one_marked_comment(tmp_path):
    from foragent.actions import flag

    tracker = FakeTracker(now=lambda: NOW)
    tracker.add_issue(1, "Issue", "body", labels=["running", "ready"])
    ctx = SimpleNamespace(store=StateStore(tmp_path / "state", now=lambda: NOW), tracker=tracker,
                          now=lambda: NOW, config=Config(), repo="owner/repo")
    flag(ctx, 1, Reason.TEST_HARNESS_CHANGED, "Held", "details", needs_human=True)
    assert {"blocked", "needs-human"} <= tracker.get_issue(1).labels
    assert not tracker.get_issue(1).labels & {"running", "ready"}
    assert ctx.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    comments = tracker.list_comments(1)
    assert len(comments) == 1 and comments[0].body.startswith(
        "<!-- foragent:needs-human reason=test-harness-changed -->")
    flag(ctx, 1, Reason.TIMEOUT, "Plain")
    assert "needs-human" in tracker.get_issue(1).labels
    assert tracker.list_comments(1)[-1].body.startswith("<!-- foragent:blocked reason=timeout -->")


def test_acceptance_comparisons_guard_the_exact_head_before_reading_evidence(monkeypatch, tmp_path):
    from foragent import ci_mode, harness_guard
    from foragent.run_attempt import ExecutorStopped

    guarded = []

    def stop(ctx, n, git, target_ref, measured, link=""):
        guarded.append((target_ref, measured))
        raise ExecutorStopped("held")

    monkeypatch.setattr(harness_guard, "guard_or_stop", stop)
    ctx = SimpleNamespace(repo="owner/repo", store=None, config=Config())
    git = SimpleNamespace(head_sha=lambda: "b" * 40)
    attempt = SimpleNamespace(n=1, ctx=ctx, git=git, target_branch="batch-7")
    with pytest.raises(ExecutorStopped):
        ci_mode.require_comparison(attempt, 9, "a" * 40)
    with pytest.raises(ExecutorStopped):
        ci_mode.review_checks(1, ctx, git, "main")
    assert guarded == [("origin/batch-7", "a" * 40), ("origin/main", "b" * 40)]


@pytest.mark.parametrize("name", ["tests/conftest.py", "pytest.ini", "tox.ini", "setup.cfg",
                                  "pkg/sitecustomize.py", "pkg/usercustomize.py", "pyproject.toml"])
def test_listed_harness_file_with_an_unreadable_blob_holds(case, name):
    """Every harness name has its blobs read: one that cannot be read cannot be compared, listed or not."""
    missing = b"1" * 40
    raw_git(case.repo, "read-tree", case.base)
    raw_git(case.repo, "update-index", "-z", "--index-info", stdin=b"100644 " + missing + b"\t" + name.encode() + b"\0")
    tree = raw_git(case.repo, "write-tree", "--missing-ok").strip().decode()
    sha = git_command(case.repo, "commit-tree", tree, "-p", case.base, "-m", "missing blob")
    held = guard_measured(case.ctx, 1, GitRepo(case.repo), "target", sha, ["**"])
    assert held == [name]
    assert case.store.get_run(1).harness_guard == {}, "a held head is never recorded as guarded"


def test_review_checks_guard_again_before_the_acceptance_ids(monkeypatch):
    """The local-only run is a measured process too; the acceptance ids measure the head as it is after it."""
    from foragent import ci_local, ci_mode, harness_guard
    from foragent.run_attempt import ExecutorStopped

    heads = iter(["a" * 40, "b" * 40])
    guarded = []

    def guard(ctx, n, git, target_ref, measured, link=""):
        guarded.append(measured)
        if len(guarded) == 2:
            raise ExecutorStopped("held")

    monkeypatch.setattr(harness_guard, "guard_or_stop", guard)
    monkeypatch.setattr(ci_local, "capture_local_only",
                        lambda *args: SimpleNamespace(command="true", node_ids=("tests/acceptance/test_x.py::t",)))
    monkeypatch.setattr(ci_local, "verify_local_only_units", lambda *args: None)
    monkeypatch.setattr(ci_mode, "_baseline_exclusions", lambda *args: [])
    ctx = SimpleNamespace(repo="owner/repo", config=Config(test_command="true"),
                          store=SimpleNamespace(log_path=lambda n: "log"))
    from pathlib import Path
    git = SimpleNamespace(head_sha=lambda: next(heads), ref_sha=lambda ref: "c" * 40, root=Path("/nonexistent"),
                          changed_paths=lambda ref: [])
    from contextlib import nullcontext
    monkeypatch.setattr("foragent.run_waits.in_phase", lambda *args: nullcontext())
    with pytest.raises(ExecutorStopped):
        ci_mode.review_checks(1, ctx, git, "main")
    assert guarded == ["a" * 40, "b" * 40]
