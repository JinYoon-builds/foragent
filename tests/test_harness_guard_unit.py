"""Boundaries of the test-harness change detector."""

import pytest

from conftest import git_command
from foragent import runner
from foragent.git import GitRepo
from foragent.harness_guard import changed_harness_paths, guard_measurements
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
