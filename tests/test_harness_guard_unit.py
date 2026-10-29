"""Boundaries of the test-harness change detector."""

import pytest

from conftest import git_command
from foragent import runner
from foragent.git import GitRepo
from foragent.harness_guard import HARNESS_NAMES, changed_harness_paths, guard_measurements
from foragent.reasons import Reason
from runner_support import run_case, set_done_when  # noqa: F401

PYPROJECT = '[project]\nname = "x"\nversion = "0"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n'


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git_command(root, "init", "-q", "--initial-branch=main")
    git_command(root, "config", "user.email", "t@example.com")
    git_command(root, "config", "user.name", "T")
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "tests").mkdir()
    (root / "tests/conftest.py").write_text("# base\n")
    commit(root, "refs/heads/main")
    git_command(root, "update-ref", "refs/heads/target", "HEAD")
    return root


def commit(root, ref, parent=None):
    """Plumbing-only commit of the working tree."""
    git_command(root, "add", "-A")
    tree = git_command(root, "write-tree").strip()
    sha = git_command(root, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", "c").strip()
    git_command(root, "update-ref", ref, sha)
    return sha


def staged(root):
    git_command(root, "add", "-A")
    return GitRepo(root)


def test_rename_reports_both_paths_and_listed_glob_is_excluded(repo):
    (repo / "tests/unit").mkdir()
    (repo / "tests/conftest.py").rename(repo / "tests/unit/conftest.py")
    git = staged(repo)
    assert changed_harness_paths(git, "target", []) == ["tests/conftest.py", "tests/unit/conftest.py"]
    assert changed_harness_paths(git, "target", ["tests/**"]) == []
    assert changed_harness_paths(git, "target", ["tests/unit/*.py"]) == ["tests/conftest.py"]


def test_target_side_harness_change_does_not_count(repo):
    (repo / "tests/conftest.py").write_text("# target moved\n")
    commit(repo, "refs/heads/target", parent=git_command(repo, "rev-parse", "HEAD").strip())
    (repo / "tests/conftest.py").write_text("# base\n")
    (repo / "feature.txt").write_text("x\n")
    assert changed_harness_paths(staged(repo), "target", []) == []


@pytest.mark.parametrize("text,held", [
    (PYPROJECT + 'dependencies = []\n', True),  # TOML: belongs to the ini_options table
    (PYPROJECT.replace('version = "0"', 'version = "1"'), False),
    (PYPROJECT + '\n[tool.pytest.ini_options.markers]\nslow = "x"\n', True),
    (PYPROJECT + '\n[tool.ruff]\nline-length = 1\n', False),
    (PYPROJECT + "\n[broken\n", True),
])
def test_pyproject_only_pytest_tables_and_unparsable_files_count(repo, text, held):
    (repo / "pyproject.toml").write_text(text)
    assert changed_harness_paths(staged(repo), "target", []) == (["pyproject.toml"] if held else [])


def test_added_nested_pyproject_with_pytest_table_and_deletion_count(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg/pyproject.toml").write_text('[tool.pytest]\naddopts = ["-q"]\n')
    (repo / "pyproject.toml").unlink()
    assert changed_harness_paths(staged(repo), "target", []) == ["pkg/pyproject.toml", "pyproject.toml"]


@pytest.mark.parametrize("change", ["add", "delete", "rename"])
def test_pytest_free_pyproject_added_deleted_or_renamed_proceeds(repo, change):
    plain = '[project]\nname = "pkg"\nversion = "0"\n'
    (repo / "pkg").mkdir()
    if change != "add":
        (repo / "pkg/pyproject.toml").write_text(plain)
        commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD").strip())
        git_command(repo, "update-ref", "refs/heads/target", "HEAD")
    if change == "add":
        (repo / "pkg/pyproject.toml").write_text(plain)
    elif change == "delete":
        (repo / "pkg/pyproject.toml").unlink()
    else:
        (repo / "lib").mkdir()
        (repo / "pkg/pyproject.toml").rename(repo / "lib/pyproject.toml")
    assert changed_harness_paths(staged(repo), "target", []) == []


@pytest.mark.parametrize("side", ["head", "base"])
def test_invalid_utf8_pyproject_counts_even_when_tables_look_equal(repo, side):
    invalid = PYPROJECT.encode() + b"# comment \xff\n"
    if side == "base":
        (repo / "pyproject.toml").write_bytes(invalid)
        commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD").strip())
        git_command(repo, "update-ref", "refs/heads/target", "HEAD")
        (repo / "pyproject.toml").write_bytes(invalid.replace(b"comment", b"note"))
    else:
        (repo / "pyproject.toml").write_bytes(invalid)
    assert changed_harness_paths(staged(repo), "target", []) == ["pyproject.toml"]


def test_non_utf8_directory_is_read_by_its_exact_bytes(repo):
    directory = repo.parent / "repo" / "caf\udcff"  # bytes b"caf\xff" on POSIX
    try:
        directory.mkdir()
    except (OSError, UnicodeEncodeError):
        pytest.skip("filesystem rejects non-UTF-8 names")
    (directory / "pyproject.toml").write_text("[tool.pytest]\n")
    assert changed_harness_paths(staged(repo), "target", []) == ["caf\ufffd/pyproject.toml"]


def test_harness_generated_by_a_done_when_command_is_held_before_later_measurements(run_case):
    c = run_case
    c.executor.steps = [{"feature.txt": "done"}]
    set_done_when(c, [
        "mkdir -p tests/gen && printf '# generated\\n' > tests/gen/conftest.py",
        'test "$(cat feature.txt)" = done',
    ])
    assert runner.run_issue(1, c.ctx) == "held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert "needs-human" in c.tracker.get_issue(1).labels
    assert c.tracker.merged_prs == []
    assert any("tests/gen/conftest.py" in comment.body for comment in c.tracker.list_comments(1)
               if "reason=test-harness-changed" in comment.body)


@pytest.mark.parametrize("before,after,held", [
    ("xfail_strict = false", "xfail_strict = 0", True),
    ("minversion = 1", "minversion = 1.0", True),
    ("timeout = 1.0", "timeout = 1.00", False),
    ("ratio = nan", "ratio = nan", False),
    ("when = 2026-01-01T00:00:00Z", "when = 2026-01-01T00:00:00+00:00", False),
    ("when = 2026-01-01T00:00:00Z", "when = 2026-01-01T00:00:00", True),
])
@pytest.mark.parametrize("table", ["tool.pytest.ini_options", "tool.pytest.ini_options.sub",
                                   "project.entry-points.pytest11"])
def test_pytest_tables_compare_toml_types_exactly(repo, before, after, held, table):
    base = '[project]\nname = "x"\nversion = "0"\n'
    (repo / "pyproject.toml").write_text(f"{base}\n[{table}]\n{before}\n")
    commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD").strip())
    git_command(repo, "update-ref", "refs/heads/target", "HEAD")
    # A change outside the pytest tables rides along; only the tables decide.
    (repo / "pyproject.toml").write_text(f"{base.replace('0', '1')}\n[{table}]\n{after}\n")
    assert changed_harness_paths(staged(repo), "target", []) == (["pyproject.toml"] if held else [])


def test_every_check_in_a_guarded_worktree_runs_the_guard_first(tmp_path):
    class Held(Exception):
        pass

    def guard():
        raise Held

    log = tmp_path / "log.txt"
    log.write_text("")
    stop = guard_measurements(tmp_path, guard)
    try:
        with pytest.raises(Held):
            runner._check("touch measured", tmp_path, log, 5)
        assert not (tmp_path / "measured").exists()
    finally:
        stop()
    assert runner._check("touch measured", tmp_path, log, 5).exit_code == 0


def test_harness_created_by_local_only_collection_is_guarded_before_selection(run_case, tmp_path):
    from types import SimpleNamespace
    from foragent.ci_local import verify_local_only_units

    c = run_case
    c.store.start_run(1, branch="foragent/issue-1", worktree=c.store.worktree_path(1))
    root = tmp_path / "head"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_marked.py").write_text("import pytest\n\n@pytest.mark.local_only\ndef test_x(): pass\n")
    script = root / "fake-pytest"
    script.write_text('#!/bin/sh\ncase "$*" in *--collect-only*) echo "# generated" > conftest.py ;;'
                      ' *) touch measured ;; esac\n')
    script.chmod(0o755)

    class Held(Exception):
        pass

    def guard():
        if (root / "conftest.py").exists():
            raise Held

    attempt = SimpleNamespace(n=1, ctx=c.ctx, worktree=root)
    stop = guard_measurements(root, guard)
    try:
        with pytest.raises(Held):
            verify_local_only_units(attempt, str(script))
    finally:
        stop()
        c.store.finish_run(1, outcome="held")
        c.tracker.remove_labels(1, ["running"])
    assert (root / "conftest.py").exists() and not (root / "measured").exists()


@pytest.mark.parametrize("restore", [True, False], ids=["reverts-target-change", "inherits-target-change"])
def test_guard_compares_with_the_current_target_not_the_pinned_start(repo, monkeypatch, restore):
    """The branch merged an advanced target; start_ref still names the old base."""
    from types import SimpleNamespace
    from foragent import run_publish
    from foragent.run_attempt import ExecutorStopped

    start = git_command(repo, "rev-parse", "HEAD").strip()
    (repo / "tests/conftest.py").write_text("# target\n")
    target = commit(repo, "refs/remotes/origin/main", parent=start)
    if restore:
        (repo / "tests/conftest.py").write_text("# base\n")
    (repo / "feature.txt").write_text("done\n")
    git_command(repo, "add", "-A")
    tree = git_command(repo, "write-tree").strip()
    merged = git_command(repo, "commit-tree", tree, "-p", start, "-p", target, "-m", "merge target").strip()
    git_command(repo, "update-ref", "refs/heads/main", merged)

    held = []
    monkeypatch.setattr(run_publish, "_append", lambda *args: None)
    monkeypatch.setattr(run_publish, "_push_branch", lambda attempt: None)
    monkeypatch.setattr(run_publish, "_open_or_update_pr", lambda *args: 7)
    monkeypatch.setattr(run_publish, "_render_pr_body", lambda attempt: "")
    monkeypatch.setattr(run_publish, "_hold_harness_change", lambda n, ctx, paths, link: held.append(paths) or "held")
    ctx = SimpleNamespace(repo="owner/repo", tracker=SimpleNamespace(get_issue=lambda n: SimpleNamespace(title="T")))
    attempt = SimpleNamespace(n=1, ctx=ctx, git=GitRepo(repo), start_ref=start, target_branch="main",
                              parsed=SimpleNamespace(files=[]), log=None)
    if restore:
        with pytest.raises(ExecutorStopped):
            run_publish._guard_harness(attempt)
        assert held == [["tests/conftest.py"]]
    else:
        run_publish._guard_harness(attempt)
        assert held == []


def test_paths_the_runner_never_stages_are_still_measured(repo):
    """stage_changes drops .foragent/ from the index; checks still load it from disk."""
    (repo / ".foragent").mkdir()
    (repo / ".foragent/conftest.py").write_text("# generated\n")
    (repo / "feature.txt").write_text("done\n")
    git = GitRepo(repo)
    git.stage_changes()
    assert ".foragent/conftest.py" not in git.changed_paths("target")
    assert changed_harness_paths(git, "target", []) == [".foragent/conftest.py"]
    # The runner's own index is left exactly as it was.
    assert ".foragent/conftest.py" not in git.changed_paths("target")


def test_force_added_ignored_conftest_is_caught(repo):
    (repo / ".gitignore").write_text("generated/\n")
    (repo / "generated").mkdir()
    (repo / "generated/conftest.py").write_text("# generated\n")
    git_command(repo, "add", ".gitignore")
    git_command(repo, "add", "-f", "generated/conftest.py")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["generated/conftest.py"]


@pytest.mark.parametrize("force_added", [True, False], ids=["staged", "on-disk-only"])
def test_ignored_pyproject_addition_with_pytest_table_is_caught(repo, force_added):
    (repo / ".gitignore").write_text("pkg/pyproject.toml\n")
    (repo / "pkg").mkdir()
    (repo / "pkg/pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-p no:cacheprovider"\n')
    git_command(repo, "add", ".gitignore")
    if force_added:
        git_command(repo, "add", "-f", "pkg/pyproject.toml")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["pkg/pyproject.toml"]


def test_committed_rename_whose_destination_is_conftest_is_caught(repo):
    (repo / "tests/unit").mkdir()
    (repo / "tests/unit/helpers.py").write_text("VALUE = 1\n" * 20)
    start = commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD").strip())
    git_command(repo, "update-ref", "refs/heads/target", start)
    (repo / "tests/unit/helpers.py").rename(repo / "tests/unit/conftest.py")
    commit(repo, "refs/heads/main", parent=start)
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["tests/unit/conftest.py"]


def test_ignored_environment_directories_are_not_expanded(repo):
    (repo / ".gitignore").write_text(".venv/\n")
    (repo / ".venv/lib/site-packages/pkg").mkdir(parents=True)
    (repo / ".venv/lib/site-packages/pkg/conftest.py").write_text("# third-party\n")
    (repo / ".venv/lib/site-packages/sitecustomize.py").write_text("# environment\n")
    git_command(repo, "add", ".gitignore")
    assert changed_harness_paths(GitRepo(repo), "target", []) == []


def test_detection_never_changes_the_index(repo):
    (repo / ".foragent").mkdir()
    (repo / ".foragent/conftest.py").write_text("# generated\n")
    git_command(repo, "add", "-f", ".foragent/conftest.py")
    before = git_command(repo, "ls-files", "--stage")
    assert changed_harness_paths(GitRepo(repo), "target", []) == [".foragent/conftest.py"]
    assert git_command(repo, "ls-files", "--stage") == before


def _replace_pyproject(repo, kind):
    import os

    path = repo / "pyproject.toml"
    path.unlink()
    if kind == "directory":
        path.mkdir()
        (path / "inside.txt").write_text("x\n")
    elif kind == "symlink-to-directory":
        os.symlink("tests", path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "dangling-symlink":
        os.symlink("missing.toml", path)


@pytest.mark.parametrize("staged", [False, True], ids=["on-disk", "staged"])
@pytest.mark.parametrize("kind", ["directory", "symlink-to-directory", "fifo", "dangling-symlink"])
def test_pyproject_replaced_by_another_file_type_is_held_not_an_error(repo, kind, staged):
    if kind == "fifo" and staged:
        pytest.skip("Git cannot stage a FIFO")
    _replace_pyproject(repo, kind)
    if staged:
        git_command(repo, "add", "-A")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["pyproject.toml"]


def test_committed_symlink_pyproject_counts_even_if_its_target_has_equal_tables(repo):
    import os

    (repo / "real.toml").write_text(PYPROJECT)
    (repo / "pyproject.toml").unlink()
    os.symlink("real.toml", repo / "pyproject.toml")
    commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD").strip())
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["pyproject.toml"]


def test_base_side_directory_named_pyproject_is_read_without_error(repo):
    (repo / "pkg/pyproject.toml").mkdir(parents=True)
    (repo / "pkg/pyproject.toml/inside.txt").write_text("x\n")
    start = commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD").strip())
    git_command(repo, "update-ref", "refs/heads/target", start)
    (repo / "pkg/pyproject.toml/inside.txt").unlink()
    (repo / "pkg/pyproject.toml").rmdir()
    (repo / "pkg/pyproject.toml").write_text('[project]\nname = "p"\n')
    git_command(repo, "add", "-A")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["pkg/pyproject.toml"]


def test_fifo_pyproject_from_the_implementer_is_held_not_a_staging_error(run_case, monkeypatch):
    """Git cannot stage a FIFO; the guard must decide before any staging runs."""
    import os

    c = run_case
    c.history.update({"pyproject.toml": PYPROJECT})
    run = c.executor.run

    def implement(**kwargs):
        result = run(**kwargs)
        path = kwargs["worktree"] / "pyproject.toml"
        path.unlink()
        os.mkfifo(path)
        return result

    monkeypatch.setattr(c.executor, "run", implement)
    assert runner.run_issue(1, c.ctx) == "held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert "needs-human" in c.tracker.get_issue(1).labels
    assert any("pyproject.toml" in comment.body for comment in c.tracker.list_comments(1)
               if "reason=test-harness-changed" in comment.body)


@pytest.mark.parametrize("commands", [
    ["rm pyproject.toml && mkfifo pyproject.toml"],
    ["rm pyproject.toml && mkfifo pyproject.toml", "true"],
], ids=["last-command-before-end-of-pass-staging", "between-measurements"])
def test_fifo_left_by_a_done_when_command_is_held_before_staging(run_case, commands):
    c = run_case
    c.history.update({"pyproject.toml": PYPROJECT})
    set_done_when(c, commands)
    assert runner.run_issue(1, c.ctx) == "held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert "needs-human" in c.tracker.get_issue(1).labels


def test_hold_survives_a_failed_publication(run_case, monkeypatch):
    from foragent import run_publish
    from foragent.git import GitError

    c = run_case
    c.executor.steps = [{"feature.txt": "done", "tests/sub/conftest.py": "# harness\n"}]

    def refuse(attempt):
        raise GitError("push refused")

    monkeypatch.setattr(run_publish, "_push_branch", refuse)
    assert runner.run_issue(1, c.ctx) == "held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    assert "needs-human" in c.tracker.get_issue(1).labels
    held = [comment.body for comment in c.tracker.list_comments(1) if "reason=test-harness-changed" in comment.body]
    assert held and "could not be published: push refused" in held[-1]


def _held_comment(c):
    held = [comment.body for comment in c.tracker.list_comments(1) if "reason=test-harness-changed" in comment.body]
    assert held, "the hold must be flagged"
    return held[-1]


def test_hold_publishes_the_unstaged_changes_it_judged(run_case):
    """The guard runs before staging; the PR must carry what it judged, not the old HEAD."""
    c = run_case
    c.executor.steps = [{"feature.txt": "done", "tests/sub/conftest.py": "# harness\n"}]
    assert runner.run_issue(1, c.ctx) == "held"
    assert "needs-human" in c.tracker.get_issue(1).labels
    assert git_command(c.history.origin, "show", "foragent/issue-1:tests/sub/conftest.py") == "# harness"
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"


def test_hold_publishes_what_git_accepts_and_names_what_it_refuses(run_case, monkeypatch):
    import os

    c = run_case
    c.history.update({"pyproject.toml": PYPROJECT})
    run = c.executor.run

    def implement(**kwargs):
        result = run(**kwargs)
        (kwargs["worktree"] / "pyproject.toml").unlink()
        os.mkfifo(kwargs["worktree"] / "pyproject.toml")
        return result

    monkeypatch.setattr(c.executor, "run", implement)
    assert runner.run_issue(1, c.ctx) == "held"
    assert git_command(c.history.origin, "show", "foragent/issue-1:feature.txt") == "done"
    assert "Git refused to stage: `pyproject.toml`" in _held_comment(c)


def test_hold_survives_a_rejected_pull_request(run_case, monkeypatch):
    from foragent.tracker import TrackerError

    c = run_case
    c.executor.steps = [{"feature.txt": "done", "tests/sub/conftest.py": "# harness\n"}]

    def reject(**kwargs):
        raise TrackerError("No commits between main and foragent/issue-1")

    monkeypatch.setattr(c.tracker, "create_pr", reject)
    assert runner.run_issue(1, c.ctx) == "held"
    assert "needs-human" in c.tracker.get_issue(1).labels
    assert "could not be published: No commits between" in _held_comment(c)


def test_hold_between_measurements_publishes_the_generated_harness(run_case):
    c = run_case
    set_done_when(c, ["mkdir -p tests/gen && echo '# generated' > tests/gen/conftest.py", "true"])
    assert runner.run_issue(1, c.ctx) == "held"
    assert git_command(c.history.origin, "show", "foragent/issue-1:tests/gen/conftest.py") == "# generated"


def test_rate_limit_while_publishing_a_hold_waits_and_holds_again_later(run_case, monkeypatch):
    from foragent.tracker import TrackerError

    c = run_case
    c.executor.steps = [{"feature.txt": "done", "tests/sub/conftest.py": "# harness\n"}]

    def limited(**kwargs):
        raise TrackerError("API rate limit exceeded")

    monkeypatch.setattr(c.tracker, "create_pr", limited)
    assert runner.run_issue(1, c.ctx) == "limit-wait"
    assert c.store.load_flag(1) is None


def _pending_merge(repo, target_files, main_files):
    """main and target both change cli.py; leave `git merge --no-commit target` pending with a conflict."""
    import subprocess

    start = git_command(repo, "rev-parse", "HEAD")
    (repo / "cli.py").write_text("base\n")
    start = commit(repo, "refs/heads/main", parent=start)
    for name, content in {"cli.py": "target\n", **target_files}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    commit(repo, "refs/heads/target", parent=start)
    git_command(repo, "read-tree", "-u", "--reset", start)  # back to the base tree, without checkout
    for name in target_files:
        if git_command(repo, "ls-tree", start, "--", name) == "":
            (repo / name).unlink(missing_ok=True)
    for name, content in {"cli.py": "main\n", **main_files}.items():
        (repo / name).write_text(content)
    commit(repo, "refs/heads/main", parent=start)
    merged = subprocess.run(["git", "merge", "--no-commit", "--no-ff", "target"], cwd=repo,
                            capture_output=True, text=True)
    assert merged.returncode != 0 and "cli.py" in merged.stdout, merged.stdout + merged.stderr
    assert (repo / ".git/MERGE_HEAD").exists()


@pytest.mark.parametrize("target_files", [
    {"tests/sub/conftest.py": "# target harness\n"},
    {"tools/pytest.ini": "[pytest]\n"},
    {"pyproject.toml": PYPROJECT.replace('"-q"', '"-q -p no:cacheprovider"')},
], ids=["conftest", "pytest-ini", "pyproject-table"])
def test_pending_merge_inherits_target_only_harness_changes(repo, target_files):
    _pending_merge(repo, target_files, {"feature.txt": "done\n"})
    assert changed_harness_paths(GitRepo(repo), "target", []) == []


def test_pending_merge_still_catches_the_prs_own_edit_of_an_inherited_harness(repo):
    _pending_merge(repo, {"tests/sub/conftest.py": "# target harness\n"}, {"feature.txt": "done\n"})
    (repo / "tests/sub/conftest.py").write_text("# PR edited the inherited harness\n")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["tests/sub/conftest.py"]


def test_pending_merge_resolved_pyproject_keeping_target_tables_is_inherited(repo):
    """main changes the version, target the pytest table; the resolution keeps both."""
    target = PYPROJECT.replace('"-q"', '"-q -p no:cacheprovider"')
    _pending_merge(repo, {"pyproject.toml": target}, {"pyproject.toml": PYPROJECT.replace('version = "0"', 'version = "1"')})
    resolved = target.replace('version = "0"', 'version = "1"')
    (repo / "pyproject.toml").write_text(resolved)
    assert changed_harness_paths(GitRepo(repo), "target", []) == []


def test_symlinked_pyproject_counts_even_when_its_link_text_parses_as_toml(repo):
    """pytest reads through the link; the link text itself must never be judged as the tables."""
    import os

    (repo / "pyproject.toml").write_text('[project]\nname = "x"\n')
    start = commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD"))
    git_command(repo, "update-ref", "refs/heads/target", start)
    (repo / "x=1").write_text('[project]\nname = "x"\n[tool.pytest.ini_options]\naddopts = "-p evil"\n')
    (repo / "pyproject.toml").unlink()
    os.symlink("x=1", repo / "pyproject.toml")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["pyproject.toml"]


def test_read_regular_never_blocks_and_follows_links_like_pytest(tmp_path):
    """Run in a child that alarms itself: a blocking open on the FIFO fails the test instead of hanging it."""
    import os
    import subprocess
    import sys

    os.mkfifo(tmp_path / "fifo.py")
    (tmp_path / "real.py").write_bytes(b"local_only\n")
    os.symlink("real.py", tmp_path / "link.py")
    (tmp_path / "dir.py").mkdir()
    script = (
        "import signal; signal.alarm(10); "  # a blocked read ends the child; tests cannot send signals
        "import sys; from pathlib import Path; from foragent.harness_guard import read_regular as r; "
        "d = Path(sys.argv[1]); "
        "print([r(d / n) for n in ('fifo.py', 'real.py', 'link.py', 'dir.py', 'missing.py')])"
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert result.stdout.strip() == "[None, b'local_only\\n', b'local_only\\n', None, None]", result.stderr


def test_guard_runs_before_the_runner_reads_worktree_files_after_measurements(run_case, monkeypatch):
    """A final Done when command leaves a FIFO pyproject; the runner must hold before reading files."""
    import sys
    from dataclasses import replace

    from foragent import ci_local, run_implementation

    c = run_case
    c.history.update({"pyproject.toml": PYPROJECT})
    c.ctx.config = replace(c.ctx.config, test_command=f"{sys.executable} -m pytest -q -p no:cacheprovider")
    monkeypatch.setattr(run_implementation, "_capture_acceptance_baseline", lambda *args: None)
    reads = []
    monkeypatch.setattr(ci_local, "verify_local_only_units", lambda *args: reads.append(args))
    set_done_when(c, ["rm pyproject.toml && mkfifo pyproject.toml"])
    assert runner.run_issue(1, c.ctx) == "held"
    assert reads == [], "the runner read worktree files before the guard held"
    assert c.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED


def test_runner_side_reads_skip_fifos_the_measured_process_left(tmp_path):
    """Non-harness names the guard does not hold on: the runner must still not block reading them."""
    import os
    import subprocess
    import sys

    (tmp_path / "tests").mkdir()
    os.mkfifo(tmp_path / "tests/test_widget.py")
    (tmp_path / "tests/test_other.py").write_text("import widget\n")
    (tmp_path / ".github/workflows").mkdir(parents=True)
    os.mkfifo(tmp_path / ".github/workflows/ci.yml")
    (tmp_path / "widget.py").write_text("VALUE = 1\n")
    script = (
        "import signal; signal.alarm(10); "  # a blocked read ends the child; tests cannot send signals
        "import sys; from pathlib import Path; d = Path(sys.argv[1]); "
        "from foragent.ci_local import _uses_local_only; "
        "from foragent.run_checks import _related_unit_tests, has_pull_request_ci; "
        "print(_uses_local_only(d), has_pull_request_ci(d), "
        "_related_unit_tests(d, ['widget.py'], 'tests/acceptance/', own_only=False))"
    )
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True)
    assert result.stdout.strip() == "False False ('tests/test_other.py',)", result.stderr


@pytest.mark.parametrize("committed", [False, True], ids=["worktree", "committed"])
@pytest.mark.parametrize("start,end", [(0o644, 0o755), (0o755, 0o644)], ids=["100644-to-100755", "100755-to-100644"])
def test_unlisted_conftest_mode_change_is_held(repo, committed, start, end):
    conftest = repo / "tests/conftest.py"
    conftest.chmod(start)
    base = commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD"))
    git_command(repo, "update-ref", "refs/heads/target", base)
    conftest.chmod(end)  # content unchanged; only the Git mode differs
    if committed:
        commit(repo, "refs/heads/main", parent=base)
    git = GitRepo(repo)
    assert changed_harness_paths(git, "target", []) == ["tests/conftest.py"]
    assert changed_harness_paths(git, "target", ["tests/conftest.py"]) == []  # a listed path proceeds


def test_mode_only_pyproject_change_is_not_a_pytest_table_change(repo):
    (repo / "pyproject.toml").chmod(0o755)
    assert changed_harness_paths(GitRepo(repo), "target", []) == []


def test_mode_is_ignored_where_git_ignores_it(repo):
    """With core.fileMode false, Git neither records nor publishes the execute bit."""
    git_command(repo, "config", "core.fileMode", "false")
    # A committed edit makes the path a candidate; the worktree restores the
    # content with the execute bit set, which Git will not publish.
    (repo / "tests/conftest.py").write_text("# edited\n")
    commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD"))
    (repo / "tests/conftest.py").write_text("# base\n")
    (repo / "tests/conftest.py").chmod(0o755)
    assert changed_harness_paths(GitRepo(repo), "target", []) == []
    (repo / "tests/conftest.py").write_text("# changed content still counts\n")
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["tests/conftest.py"]


def _without_file_mode(repo, files=None):
    """core.fileMode false, with the given files committed at mode 100644 as the merge base."""
    git_command(repo, "config", "core.fileMode", "false")
    for name, content in (files or {}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    base = commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD"))
    git_command(repo, "update-ref", "refs/heads/target", base)
    return base


@pytest.mark.parametrize("committed", [True, False], ids=["committed", "staged"])
@pytest.mark.parametrize("name", ["tests/conftest.py", "tools/pytest.ini", "setup.cfg", "pkg/sitecustomize.py"])
def test_recorded_mode_change_counts_when_git_ignores_the_execute_bit(repo, name, committed):
    """`git update-index --chmod=+x` records 100755 even with core.fileMode false; that is published."""
    base = _without_file_mode(repo, {name: "# harness\n"})
    git_command(repo, "update-index", "--chmod=+x", "--", name)
    if committed:
        tree = git_command(repo, "write-tree")
        git_command(repo, "update-ref", "refs/heads/main",
                    git_command(repo, "commit-tree", tree, "-p", base, "-m", "chmod"))
    assert git_command(repo, "ls-files", "-s", "--", name).startswith("100755")
    assert changed_harness_paths(GitRepo(repo), "target", []) == [name]


@pytest.mark.parametrize("change", ["add", "rename"])
def test_pytest_free_pyproject_is_regular_when_git_ignores_the_execute_bit(repo, change):
    plain = '[project]\nname = "pkg"\nversion = "0"\n'
    _without_file_mode(repo, {"pkg/pyproject.toml": plain} if change == "rename" else None)
    if change == "add":
        (repo / "lib").mkdir()
        (repo / "lib/pyproject.toml").write_text(plain)
    else:
        (repo / "lib").mkdir()
        (repo / "pkg/pyproject.toml").rename(repo / "lib/pyproject.toml")
    git_command(repo, "add", "-A")
    assert changed_harness_paths(GitRepo(repo), "target", []) == []
    (repo / "lib/pyproject.toml").write_text(plain + '[tool.pytest.ini_options]\naddopts = "-q"\n')
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["lib/pyproject.toml"]


def _git_bytes(repo, *args, stdin=b""):
    import subprocess

    return subprocess.run(["git", *args], cwd=repo, input=stdin, capture_output=True, check=True).stdout


def _base_with_entries(repo, entries):
    """Commit raw (mode, path bytes, blob content or sha) index entries as the merge base, then delete
    them in HEAD; no worktree file is needed, so any path bytes work on any filesystem."""
    lines = b""
    for mode, path, content in entries:
        sha = content if mode == b"160000" else _git_bytes(repo, "hash-object", "-w", "--stdin", stdin=content).strip()
        lines += mode + b" " + sha + b"\t" + path + b"\0"
    _git_bytes(repo, "update-index", "-z", "--index-info", stdin=lines)
    tree = _git_bytes(repo, "write-tree").strip().decode()
    parent = git_command(repo, "rev-parse", "HEAD")
    base = git_command(repo, "commit-tree", tree, "-p", parent, "-m", "base")
    git_command(repo, "update-ref", "refs/heads/target", base)
    removal = b"".join(b"0 " + b"0" * 40 + b"\t" + path + b"\0" for _, path, _ in entries)
    _git_bytes(repo, "update-index", "-z", "--index-info", stdin=removal)
    head = git_command(repo, "commit-tree", _git_bytes(repo, "write-tree").strip().decode(), "-p", base, "-m", "delete")
    git_command(repo, "update-ref", "refs/heads/main", head)


@pytest.mark.parametrize("name", sorted(HARNESS_NAMES | {"pyproject.toml"}))
@pytest.mark.parametrize("files", [[], ["caf�/{name}"], ["**/{name}"], ["*/*"], ["**"]],
                         ids=["unlisted", "replacement-char-entry", "glob", "star-star", "everything"])
def test_non_utf8_harness_path_always_holds_and_is_c_quoted(repo, name, files):
    """b"caf\\xff/<name>" must never match a Files entry through replacement decoding."""
    raw = b"caf\xff/" + name.encode()
    _base_with_entries(repo, [(b"100644", raw, b"# harness\n")])
    held = changed_harness_paths(GitRepo(repo), "target", [entry.format(name=name) for entry in files])
    assert held == [f'"caf\\377/{name}"']
    # The same C-quoted form Git itself prints for that path.
    quoted = _git_bytes(repo, "-c", "core.quotePath=true", "ls-tree", "--name-only", "-r", "target").decode()
    assert held[0] in quoted.splitlines()


def test_files_entries_match_raw_bytes_exactly(repo):
    """An NFD path is not listed by an NFC entry that decodes to the same text."""
    import unicodedata

    nfd = unicodedata.normalize("NFD", "café/conftest.py").encode()
    nfc = unicodedata.normalize("NFC", "café/conftest.py")
    assert nfd != nfc.encode()
    _base_with_entries(repo, [(b"100644", nfd, b"# harness\n")])
    git = GitRepo(repo)
    assert changed_harness_paths(git, "target", [nfc]) == [nfd.decode()]
    assert changed_harness_paths(git, "target", [nfd.decode()]) == []  # exact bytes: listed
    assert changed_harness_paths(git, "target", ["caf*/conftest.py"]) == []  # byte glob


@pytest.mark.parametrize("mode", [b"120000", b"160000"], ids=["symlink", "submodule"])
@pytest.mark.parametrize("name", ["tests/conftest.py", "pyproject.toml"])
def test_symlink_or_submodule_on_the_base_side_always_holds_even_listed(repo, mode, name):
    content = b"a" * 40 if mode == b"160000" else b"elsewhere.py"
    _base_with_entries(repo, [(mode, name.encode(), content)])
    for files in ([], [name], ["**"]):
        assert changed_harness_paths(GitRepo(repo), "target", files) == [name]


@pytest.mark.parametrize("head", ["regular-to-symlink", "symlink-retargeted"])
def test_symlink_on_the_head_side_always_holds_even_listed(repo, head):
    import os

    conftest = repo / "tests/conftest.py"
    if head == "symlink-retargeted":
        conftest.unlink()
        os.symlink("a.py", conftest)
        base = commit(repo, "refs/heads/main", parent=git_command(repo, "rev-parse", "HEAD"))
        git_command(repo, "update-ref", "refs/heads/target", base)
        conftest.unlink()
        os.symlink("b.py", conftest)
    else:
        conftest.unlink()
        os.symlink("elsewhere.py", conftest)
    assert changed_harness_paths(GitRepo(repo), "target", ["tests/conftest.py"]) == ["tests/conftest.py"]


def test_listed_regular_harness_file_may_change_content_and_mode(repo):
    conftest = repo / "tests/conftest.py"
    conftest.write_text("# listed change\n")
    conftest.chmod(0o755)
    git = GitRepo(repo)
    assert changed_harness_paths(git, "target", ["tests/conftest.py"]) == []
    assert changed_harness_paths(git, "target", []) == ["tests/conftest.py"]


def test_unparsable_pyproject_always_holds_even_listed(repo):
    (repo / "pyproject.toml").write_text("[broken\n")
    assert changed_harness_paths(GitRepo(repo), "target", ["pyproject.toml"]) == ["pyproject.toml"]


def test_unreadable_base_blob_always_holds_even_listed(repo):
    missing = b"1" * 40
    tree = _git_bytes(repo, "mktree", "--missing", stdin=b"100644 blob " + missing + b"\tconftest.py\n").strip().decode()
    base = git_command(repo, "commit-tree", tree, "-m", "base with a missing blob")
    git_command(repo, "update-ref", "refs/heads/target", base)
    git_command(repo, "update-ref", "refs/heads/main",
                git_command(repo, "commit-tree", _git_bytes(repo, "write-tree").strip().decode(), "-p", base, "-m", "head"))
    # The head's other files are listed additions; only the unreadable blob cannot be compared.
    files = ["conftest.py", "pyproject.toml", "tests/conftest.py"]
    assert changed_harness_paths(GitRepo(repo), "target", files) == ["conftest.py"]


def test_flag_needs_human_blocks_and_hands_over_with_one_marked_comment(tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from foragent.actions import flag
    from foragent.config import Config
    from foragent.state import StateStore
    from foragent.tracker import FakeTracker

    now = datetime(2026, 10, 29, tzinfo=UTC)
    tracker = FakeTracker(now=lambda: now)
    tracker.add_issue(1, "Issue", "body", labels=["running", "ready"])
    ctx = SimpleNamespace(store=StateStore(tmp_path / "state", now=lambda: now), tracker=tracker,
                          now=lambda: now, config=Config(), repo="owner/repo")
    flag(ctx, 1, Reason.TEST_HARNESS_CHANGED, "Held", "details", needs_human=True)
    assert {"blocked", "needs-human"} <= tracker.get_issue(1).labels
    assert not tracker.get_issue(1).labels & {"running", "ready"}
    assert ctx.store.load_flag(1).reason == Reason.TEST_HARNESS_CHANGED
    comments = tracker.list_comments(1)
    assert len(comments) == 1
    assert comments[0].body.startswith("<!-- foragent:needs-human reason=test-harness-changed -->")
    flag(ctx, 1, Reason.TIMEOUT, "Plain", None)
    assert tracker.list_comments(1)[-1].body.startswith("<!-- foragent:blocked reason=timeout -->")


@pytest.mark.parametrize("committed", [False, True], ids=["staged", "committed"])
def test_recorded_symlink_with_identical_bytes_holds_when_checked_out_as_a_file(repo, committed):
    """core.symlinks false: Git records 120000 while the worktree keeps a regular file with the same bytes."""
    git_command(repo, "config", "core.symlinks", "false")
    sha = git_command(repo, "rev-parse", "HEAD:tests/conftest.py")
    git_command(repo, "update-index", "--cacheinfo", f"120000,{sha},tests/conftest.py")
    if committed:
        tree = git_command(repo, "write-tree")
        git_command(repo, "update-ref", "refs/heads/main",
                    git_command(repo, "commit-tree", tree, "-p", "HEAD", "-m", "symlink mode"))
    assert (repo / "tests/conftest.py").read_text() == "# base\n"  # the worktree still shows a file
    for files in ([], ["tests/conftest.py"]):
        assert changed_harness_paths(GitRepo(repo), "target", files) == ["tests/conftest.py"]


def test_skip_worktree_entry_is_judged_by_what_the_index_publishes(repo):
    """Staging never refreshes a skip-worktree entry from disk; its index content is what a commit carries."""
    sha = _git_bytes(repo, "hash-object", "-w", "--stdin", stdin=b"# changed only in the index\n").strip().decode()
    git_command(repo, "update-index", "--cacheinfo", f"100644,{sha},tests/conftest.py")
    git_command(repo, "update-index", "--skip-worktree", "tests/conftest.py")
    assert (repo / "tests/conftest.py").read_text() == "# base\n"
    git = GitRepo(repo)
    assert changed_harness_paths(git, "target", []) == ["tests/conftest.py"]
    assert changed_harness_paths(git, "target", ["tests/conftest.py"]) == []  # listed regular change


@pytest.mark.parametrize("entry", ["/tests/conftest.py", "./tests/conftest.py", "tests//conftest.py",
                                   "tests/./conftest.py", "tests/conftest.py/"])
def test_files_entries_are_not_normalized(repo, entry):
    """Only equal bytes (or a byte glob) list a path; a differently spelled entry authorizes nothing."""
    (repo / "tests/conftest.py").write_text("# changed\n")
    git = GitRepo(repo)
    assert changed_harness_paths(git, "target", [entry]) == ["tests/conftest.py"]
    assert changed_harness_paths(git, "target", ["tests/conftest.py"]) == []


def test_symlink_recorded_only_in_head_holds_when_the_index_dropped_it(repo):
    """The runner unstages .foragent/ (rm --cached); HEAD still records the PR head's symlink there."""
    git_command(repo, "config", "core.symlinks", "false")
    sha = _git_bytes(repo, "hash-object", "-w", "--stdin", stdin=b"../tests/conftest.py").strip().decode()
    git_command(repo, "update-index", "--add", "--cacheinfo", f"120000,{sha},.foragent/conftest.py")
    tree = git_command(repo, "write-tree")
    git_command(repo, "update-ref", "refs/heads/main",
                git_command(repo, "commit-tree", tree, "-p", "HEAD", "-m", "head symlink"))
    git_command(repo, "rm", "-q", "--cached", ".foragent/conftest.py")
    (repo / ".foragent").mkdir()
    (repo / ".foragent/conftest.py").write_text("../tests/conftest.py")  # core.symlinks false checkout
    assert changed_harness_paths(GitRepo(repo), "target", [".foragent/conftest.py"]) == [".foragent/conftest.py"]


@pytest.mark.parametrize("files", [[], ["pyproject.toml"]], ids=["unlisted", "listed"])
def test_deeply_nested_pyproject_arrays_hold_instead_of_erroring(repo, files):
    (repo / "pyproject.toml").write_text("[tool.pytest]\nx = " + "[" * 2000 + "]" * 2000 + "\n")
    assert changed_harness_paths(GitRepo(repo), "target", files) == ["pyproject.toml"]


def test_deeply_nested_pyproject_tables_are_compared_without_recursion(repo):
    header = "[tool.pytest" + "".join(f".k{i}" for i in range(2000)) + "]\nx = 1\n"
    (repo / "pyproject.toml").write_text(PYPROJECT + header)
    assert changed_harness_paths(GitRepo(repo), "target", []) == ["pyproject.toml"]


def test_canonical_form_of_a_deep_value_is_flat_and_comparable():
    from foragent.harness_guard import _canonical

    deep = value = {}
    for _ in range(5000):
        value["k"] = [{}]
        value = value["k"][0]
    assert _canonical(deep) == _canonical(deep)
    assert _canonical({"a": 0}) != _canonical({"a": False}) != _canonical({"a": [0]})


def test_files_glob_matching_handles_thousands_of_path_components():
    from foragent.harness_guard import listed

    path = b"a/" * 3000 + b"conftest.py"
    assert listed(path, ["**/conftest.py"])
    assert not listed(path, ["a/*/conftest.py"])
    assert listed(b"a/b/conftest.py", ["a/**/conftest.py"]) and listed(b"a/conftest.py", ["a/**/conftest.py"])


def test_any_failure_while_judging_a_harness_path_holds_it(repo, monkeypatch):
    from foragent import harness_guard

    (repo / "tests/conftest.py").write_text("# changed\n")

    def broken(*args):
        raise RuntimeError("cannot compare")

    monkeypatch.setattr(harness_guard, "_tree_side", broken)
    assert changed_harness_paths(GitRepo(repo), "target", ["tests/conftest.py"]) == ["tests/conftest.py"]


def test_too_deep_to_parse_is_unparsable_not_an_exception():
    from foragent.harness_guard import _UNPARSABLE, _pytest_tables

    deep = ("[tool.pytest]\nx = " + "[" * 2000 + "]" * 2000 + "\n").encode()
    assert _pytest_tables(deep) is _UNPARSABLE
