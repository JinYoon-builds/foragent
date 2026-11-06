"""Related checks subtract only exact node IDs observed passing in CI."""

import shlex
import sys

import pytest

from conftest import BASE_TIME
from foragent.config import Config
from foragent.git import GitRepo
from foragent.run_checks import _pytest_command, run_related_check
from foragent.runner import RunContext
from foragent.state import StateStore
from foragent.tracker import FakeTracker


@pytest.fixture
def related(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    (root / "tests").mkdir(parents=True)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="test", worktree=root)
    ctx = RunContext("owner/repo", root, FakeTracker(), store,
                     Config(checks="ci", merge_owner=True), GitRepo(root), lambda: BASE_TIME)
    command = _pytest_command(f"{sys.executable} -m pytest")
    return root, ctx, command


def test_partial_ci_passes_keep_every_unlisted_node_even_after_long_collection(related):
    root, ctx, command = related
    file = "tests/test_many.py"
    (root / file).write_text(
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('number', range(160))\n"
        "def test_value(number):\n"
        "    assert number == 0, 'already passed in CI'\n"
        "    Path('executed').write_text(str(number))\n",
    )
    passed = {f"{file}::test_value[{number}]" for number in range(1, 160)}

    result = run_related_check(1, ctx, root, command, [file], 1, 60,
                               ci_passed=(42, passed), head_sha="head")

    assert result.exit_code == 0, result.output
    assert (root / "executed").read_text() == "0"
    assert "-p _foragent_residual_" in result.command
    assert file in shlex.split(result.command)
    assert "related units: skipped 159 of 160 using CI run 42 for head" in ctx.store.log_path(1).read_text()
    record, = ctx.store.get_run(1).related_tests
    assert record["selected_files"] == [file]


def test_all_ci_passes_record_exact_run_and_head_without_running_tests(related):
    root, ctx, command = related
    file = "tests/test_covered.py"
    (root / file).write_text("def test_covered():\n    assert False, 'must not run'\n")

    result = run_related_check(1, ctx, root, command, [file], 1, 60,
                               ci_passed=(42, {f"{file}::test_covered"}), head_sha="exact-sha")

    assert result.exit_code == 0
    assert "related units: all 1 passed in CI run 42 for exact-sha" in ctx.store.log_path(1).read_text()
    assert "related units: skipped 1 of 1 using CI run 42 for exact-sha" in ctx.store.log_path(1).read_text()
    record, = ctx.store.get_run(1).related_tests
    assert not record["timed_out"]
    assert record["file_seconds"] == {}


@pytest.mark.parametrize("evidence", [None, (42, set())], ids=["unavailable", "no-passed-nodes"])
def test_no_ci_passes_run_every_node(related, evidence):
    root, ctx, command = related
    file = "tests/test_required.py"
    (root / file).write_text("def test_required():\n    assert False, 'local execution required'\n")

    result = run_related_check(1, ctx, root, command, [file], 1, 60,
                               ci_passed=evidence, head_sha="head")

    assert result.exit_code == 1
    assert "local execution required" in result.output
    assert shlex.split(result.command)[-1] == file


@pytest.mark.parametrize("skip_one", [False, True], ids=["no-passes", "partial-pass"])
def test_large_parameterized_residual_executes_without_expanding_the_command(related, skip_one):
    root, ctx, command = related
    file = "tests/test_large.py"
    (root / file).write_text(
        "from pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('number', range(6000), ids=lambda n: 'x' * 200 + str(n))\n"
        "def test_value(number):\n"
        "    with Path('executed').open('a') as stream: stream.write(str(number) + '\\n')\n")
    passed = {f"{file}::test_value[{'x' * 200}0]"} if skip_one else set()

    result = run_related_check(1, ctx, root, command, [file], 1, 60,
                               ci_passed=(42, passed), head_sha="head")

    assert result.exit_code == 0, result.output
    assert (root / "executed").read_text().splitlines() == [str(n) for n in range(int(skip_one), 6000)]
    assert len(result.command.encode()) < 4096
    assert file in shlex.split(result.command)
    assert ("-p _foragent_residual_" in result.command) is skip_one
    assert f"related units: skipped {int(skip_one)} of 6000 using CI run 42 for head" in (
        ctx.store.log_path(1).read_text())


@pytest.mark.parametrize("failure", ["collection-error", "missing-listing", "timeout", "runner-error"])
def test_collection_problems_fall_back_to_original_file_check(related, monkeypatch, failure):
    from foragent import runner
    from foragent.run_checks import CheckResult

    root, ctx, command = related
    file = "tests/test_fallback.py"
    calls = []

    def check(invocation, *args, **kwargs):
        calls.append(invocation)
        collecting = "--collect-only" in invocation
        if collecting and failure == "runner-error":
            raise RuntimeError("collection process unavailable")
        return CheckResult(invocation, 2 if collecting and failure == "collection-error" else 0,
                           collecting and failure == "timeout", "")

    monkeypatch.setattr(runner, "_check", check)

    result = run_related_check(1, ctx, root, command, [file], 1, 60,
                               ci_passed=(42, {f"{file}::test_fallback"}), head_sha="head")

    assert result.exit_code == 0
    assert len(calls) == 2
    assert "--collect-only" in calls[0]
    assert "--collect-only" not in calls[1]
    assert calls[1].endswith(file)
