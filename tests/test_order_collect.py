"""The collection check's targets, isolation, budget, base choice and fail-closed causes (#1523)."""

from dataclasses import replace
from pathlib import Path
import sys
import tempfile

import pytest

from foragent import capacity, order, order_collect, order_flush, processes
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.state import StateStore
from foragent.tracker import FakeTracker

from conftest import git_command
from test_order import offline_publication  # noqa: F401


GOOD = "tests/acceptance/test_good.py"
HELPER = "tests/acceptance/collect_helper.py"
IMPORTS = "from collect_helper import VALUE\n\ndef test_value():\n    assert VALUE\n"
FLUSH = {"repo": "owner/repo", "boundary": "b"}


def plan_with(files, key="only", **extra):
    return {"id": f"plan-{key}", "title": "T", "summary": "S", "spec_markdown": "# S\n", "issues": [{
        "key": key, "title": "T", "lane": "main", "size": "s", "goal": "G", "out_of_scope": "O",
        "done_when": [f"uv run pytest -q {next(iter(files))}"] if files else ["true"],
        "acceptance_files": files, **extra}]}


@pytest.fixture
def case(git_history, tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    # Temporary folders of this test only, so what the check leaves behind can be listed.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    root = git_history.clone
    config = Config(test_command=f"{sys.executable} -m pytest", test_slots=1, language="en")
    return root, config, GitRepo(root), git_history, scratch


def check(case, files, *, command=None, **options):
    root, config, git, _, _ = case
    config = replace(config, test_command=command) if command else config
    plan = plan_with(files)
    return order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git, **options)


def slot_is_free():
    stream = capacity._take_test_slot(1, wait=False)
    if stream is None:
        return False
    stream.close()
    return True


def _publish_case(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, calls, origin = offline_publication
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(order, "require_clean_preread", lambda *a, **k: None)
    config = replace(config, language="en", test_command=f"{sys.executable} -m pytest")
    return root, config, tracker, git, calls, origin


def _checkout(root, git):
    return git.head_sha(), git_command(root, "ls-files", "--stage"), (root / HELPER).read_text()


def _helper_on_main(git_history, git):
    git_history.update({HELPER: "VALUE = 1\n"})
    git.update_base("main")


def _writes(calls):
    return [args for args in calls if "commit" in args or args[:1] == ["push"]]


def test_a_plan_without_acceptance_files_touches_nothing(case, monkeypatch):
    root, config, *_ = case

    class NoGit:
        def __getattr__(self, name):
            pytest.fail(f"no base is needed without acceptance files: {name}")

    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("nothing to collect"))
    plan = plan_with({})
    assert order_collect.check(plan, order.validate_plan(plan, root, config), root, config, NoGit()) is None


def test_helper_without_test_functions_collects_and_leaves_nothing(case):
    root, _, _, _, scratch = case
    assert check(case, {HELPER: "VALUE = 1\n"}).commit == git_command(root, "rev-parse", "refs/remotes/origin/main")
    assert list(scratch.iterdir()) == []
    assert slot_is_free()


def test_import_error_names_file_and_cause_then_cleans_up(case):
    *_, scratch = case
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: IMPORTS})
    message = str(refusal.value)
    assert GOOD in message and "ModuleNotFoundError" in message and "collect_helper" in message
    assert str(scratch) not in message and "unavailable" not in message
    assert list(scratch.iterdir()) == []
    assert slot_is_free()


@pytest.mark.parametrize("done_when", [
    "uv run pytest -q tests/acceptance/test_good.py",
    "cat tests/acceptance/custom_helper.py && python -m pytest tests/acceptance/test_good.py",
    "python -X tests/acceptance/custom_helper.py -c 'print(1)'",
    "python tests/acceptance/custom_helper.py",
], ids=["unnamed", "cat", "option-value", "script"])
def test_every_published_python_file_is_collected_whatever_done_when_says(case, done_when):
    root, config, git, *_ = case
    custom = "tests/acceptance/custom_helper.py"
    plan = plan_with({GOOD: "VALUE = 1\n", custom: "import no_such_module_anywhere\n"})
    plan["issues"][0]["done_when"] = [done_when]
    with pytest.raises(order.PlanError) as refusal:
        order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git)
    assert custom in str(refusal.value) and "no_such_module_anywhere" in str(refusal.value)


def test_files_resolved_from_done_when_are_collected_too(case):
    root, config, git, *_ = case
    resolved = "tests/acceptance/resolved_only.py"
    (root / resolved).parent.mkdir(parents=True, exist_ok=True)
    (root / resolved).write_text("import no_such_module_anywhere\n")
    plan = plan_with({})
    plan["issues"][0]["done_when"] = [f"uv run pytest -q {resolved}"]
    with pytest.raises(order.PlanError, match="no_such_module_anywhere"):
        order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git)


def test_a_broken_published_conftest_is_refused_with_its_path_and_error(case):
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "VALUE = 1\n", "tests/acceptance/conftest.py": "import no_such_module_anywhere\n"})
    assert "tests/acceptance/conftest.py" in str(refusal.value) and "no_such_module_anywhere" in str(refusal.value)


def test_only_listed_files_join_the_base_and_inherited_paths_are_gone(case, monkeypatch, tmp_path):
    root, *_ = case
    (root / HELPER).parent.mkdir(parents=True, exist_ok=True)
    (root / HELPER).write_text("VALUE = 1\n")
    elsewhere = tmp_path / "caller-path"
    elsewhere.mkdir()
    (elsewhere / "collect_helper.py").write_text("VALUE = 1\n")
    (elsewhere / "caller_plugin.py").write_text("raise RuntimeError('caller plugin loaded')\n")
    monkeypatch.setenv("PYTHONPATH", str(elsewhere))
    monkeypatch.setenv("PYTEST_PLUGINS", "caller_plugin")
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        check(case, {GOOD: IMPORTS})
    assert check(case, {GOOD: IMPORTS, HELPER: "VALUE = 2\n"}) is not None


@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONUSERBASE", "PYTEST_PLUGINS", "UV_PROJECT", "UV_WORKING_DIRECTORY"])
def test_each_inherited_import_setting_is_gone_for_the_command(case, monkeypatch, tmp_path, name):
    tool = tmp_path / "tool"
    tool.mkdir()
    seen = tmp_path / "seen"
    (tool / "pytest").write_text(f"#!/bin/sh\nenv > {seen}\nexit 5\n")
    (tool / "pytest").chmod(0o755)
    monkeypatch.setenv(name, "/caller")
    with pytest.raises(order.PlanError, match="unavailable"):  # This launcher writes no report.
        check(case, {GOOD: "VALUE = 1\n"}, command=str(tool / "pytest"))
    assert f"{name}=/caller" not in seen.read_text()


def test_the_bases_own_pytest_configuration_applies_inside_the_copy(case):
    root, _, git, history, _ = case
    history.update({"pyproject.toml": "[tool.pytest.ini_options]\npythonpath = ['src']\n",
                    "src/collect_helper.py": "VALUE = 1\n"})
    git.update_base("main")
    assert check(case, {GOOD: IMPORTS}) is not None


def test_a_wrapper_launcher_outside_the_copy_is_the_projects_choice(case, tmp_path):
    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "pytest").write_text(f"#!{sys.executable}\nimport pytest, sys\nsys.exit(pytest.main(sys.argv[1:]))\n")
    (tool / "pytest").chmod(0o755)
    assert check(case, {GOOD: "VALUE = 1\n"}, command=str(tool / "pytest")) is not None


@pytest.mark.parametrize("words", [
    "PYTHONPATH={outside} {python} -m pytest",
    "PYTHONPATH=src:{outside} {python} -m pytest",
    "env PYTHONPATH={outside} {python} -m pytest",
    "{python} -m pytest -o pythonpath={outside}",
    "{python} -m pytest --override-ini=pythonpath={outside}",
])
def test_a_helper_only_on_a_path_the_command_adds_cannot_satisfy_an_import(case, tmp_path, words):
    outside = tmp_path / "caller-only"
    outside.mkdir()
    (outside / "collect_helper.py").write_text("VALUE = 1\n")
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: IMPORTS}, command=words.format(outside=outside, python=sys.executable))
    assert GOOD in str(refusal.value) and "ModuleNotFoundError" in str(refusal.value)
    assert "collect_helper" in str(refusal.value)


def test_a_helper_on_a_committed_absolute_pythonpath_cannot_satisfy_an_import(case, tmp_path):
    _, _, git, history, _ = case
    outside = tmp_path / "caller-only"
    outside.mkdir()
    (outside / "collect_helper.py").write_text("VALUE = 1\n")
    history.update({"pyproject.toml": f"[tool.pytest.ini_options]\npythonpath = ['{outside}']\n"})
    git.update_base("main")
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        check(case, {GOOD: IMPORTS})


def test_a_quoted_import_path_in_the_command_is_kept_as_written(case):
    _, _, git, history, _ = case
    history.update({"src code/collect_helper.py": "VALUE = 1\n"})
    git.update_base("main")
    assert check(case, {GOOD: IMPORTS}, command=f"PYTHONPATH='src code' {sys.executable} -m pytest") is not None


def test_a_path_inside_the_clean_copy_stays_the_projects_choice(case):
    root, _, git, history, _ = case
    history.update({"src/collect_helper.py": "VALUE = 1\n"})
    git.update_base("main")
    command = f"PYTHONPATH=src {sys.executable} -m pytest"
    assert check(case, {GOOD: IMPORTS}, command=command) is not None
    (root / "caller_only").mkdir()
    (root / "caller_only/other_helper.py").write_text("VALUE = 1\n")
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        check(case, {GOOD: "from other_helper import VALUE\n"}, command=f"PYTHONPATH=caller_only {sys.executable} -m pytest")


def test_a_test_exiting_at_import_is_refused_and_named(case):
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "raise SystemExit(1)\n"})
    assert GOOD in str(refusal.value) and "SystemExit" in str(refusal.value)
    assert "unavailable" not in str(refusal.value)


def test_runtime_red_is_not_executed(case):
    root, *_ = case
    witness = root.parent / "ran"
    body = f"def test_red():\n    open({str(witness)!r}, 'w').close()\n    assert False\n"
    assert check(case, {GOOD: body}) is not None
    assert not witness.exists()


def test_timeout_is_unavailable_and_execution_gets_only_the_budget_left(case, monkeypatch):
    calls = []

    def timed_out(argv, *, timeout_s, **kwargs):
        calls.append((argv, timeout_s))
        return None, True, ""

    monkeypatch.setattr(processes, "run_logged", timed_out)
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "VALUE = 1\n"})
    message = str(refusal.value)
    assert "unavailable" in message and GOOD in message and "--collect-only" in message and "30 seconds" in message
    assert 0 < calls[0][1] <= order_collect.LIMIT_S
    assert slot_is_free()
    calls.clear()
    with pytest.raises(order.PlanError):
        check(case, {GOOD: "VALUE = 1\n"}, earlier=order_collect.Collected("not-this-base", order_collect.LIMIT_S - 1))
    assert 0 < calls[0][1] <= 1


def test_spent_budget_starts_nothing(case, monkeypatch):
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("no budget is left"))
    with pytest.raises(order.PlanError, match="unavailable"):
        check(case, {GOOD: "VALUE = 1\n"}, earlier=order_collect.Collected("not-this-base", order_collect.LIMIT_S))


def test_held_slots_do_not_delay_or_change_the_answer(case):
    """Lane test runs holding every machine test slot neither delay collection nor change its answer."""
    held = capacity._take_test_slot(1, wait=False)
    assert held is not None
    try:
        assert check(case, {HELPER: "VALUE = 1\n"}).spent < order_collect.LIMIT_S
        with pytest.raises(order.PlanError) as refusal:
            check(case, {GOOD: IMPORTS})
        assert not slot_is_free()
    finally:
        held.close()
    message = str(refusal.value)
    assert "ModuleNotFoundError" in message and GOOD in message and "unavailable" not in message


@pytest.mark.parametrize("command,cause", [
    ("npm test", "not a pytest command"),
    ("/__missing__/pytest", "code 127"),
    # A launcher that exits 0 without a report established nothing.
    ("/usr/bin/true pytest", "no collection result"),
])
def test_commands_that_cannot_collect_are_unavailable(case, command, cause):
    *_, scratch = case
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "VALUE = 1\n"}, command=command)
    assert "unavailable" in str(refusal.value) and cause in str(refusal.value)
    assert list(scratch.iterdir()) == []


def test_temporary_folder_failure_is_unavailable_not_a_raw_error(case, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", refused)
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "VALUE = 1\n"})
    assert "unavailable" in str(refusal.value) and "No space left" in str(refusal.value) and GOOD in str(refusal.value)


def test_no_command_is_detected_from_the_clean_base_not_the_caller(case):
    root, config, git, history, scratch = case
    (root / "pyproject.toml").write_text("[project]\nname = 'caller-only'\n")
    without = (root, replace(config, test_command=None), git, history, scratch)
    with pytest.raises(order.PlanError, match="no test command"):
        check(without, {GOOD: "VALUE = 1\n"})


def test_detected_command_comes_from_the_base(case, monkeypatch):
    root, config, git, history, scratch = case
    history.update({"pyproject.toml": "[project]\nname = 'base'\nversion = '0'\n"})
    git.update_base("main")
    seen = []

    def report(argv, *, cwd, log_path, **kwargs):
        seen.append(argv[-1])
        return 127, False, ""

    monkeypatch.setattr(processes, "run_logged", report)
    with pytest.raises(order.PlanError, match="unavailable"):
        check((root, replace(config, test_command=None), git, history, scratch), {GOOD: "VALUE = 1\n"})
    assert "PYTEST_ADDOPTS= uv run pytest " in seen[0] and "--collect-only" in seen[0]


def test_a_data_file_no_collector_knows_passes(case):
    assert check(case, {"tests/acceptance/data.json": "{}\n"}) is not None


def test_a_published_text_file_is_collected_by_pytest_as_a_doctest(case):
    """pytest collects a named .txt, and this one raises ValueError while it does."""
    spec = "tests/acceptance/spec.txt"
    with pytest.raises(order.PlanError) as refusal:
        check(case, {spec: ">>>not_space\n"})
    assert spec in str(refusal.value) and "ValueError" in str(refusal.value)


def test_a_file_the_projects_options_collect_is_checked_whatever_its_suffix(case):
    """A configured doctest glob makes pytest collect a Markdown file, and this one fails to."""
    spec = "tests/acceptance/spec.md"
    with pytest.raises(order.PlanError) as refusal:
        check(case, {spec: ">>>not_space\n"}, command=f"{sys.executable} -m pytest --doctest-glob=*.md")
    assert spec in str(refusal.value) and "ValueError" in str(refusal.value)
    assert check(case, {spec: ">>>not_space\n"}) is not None, "without that option nothing collects it"


def test_same_base_is_not_collected_twice_and_a_new_one_is(case, monkeypatch):
    _, _, git, history, _ = case
    first = check(case, {GOOD: "VALUE = 1\n"})
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("this base is already checked"))
    assert check(case, {GOOD: "VALUE = 1\n"}, earlier=first) is first
    monkeypatch.undo()
    history.update({"unrelated.txt": "moved\n"})
    second = check(case, {GOOD: "VALUE = 1\n"}, earlier=order_collect.Collected(first.commit, 5.0))
    assert second.commit != first.commit and second.spent > 5.0


def test_base_is_the_commit_main_would_receive(case):
    root, _, git, history, _ = case
    # Behind the remote: the remote decides, and the checkout stays where it is.
    history.update({HELPER: "VALUE = 1\n"})
    before = git.head_sha()
    assert check(case, {GOOD: IMPORTS}).commit == git_command(history.origin, "rev-parse", "refs/heads/main")
    assert git.head_sha() == before and not (root / HELPER).exists()
    # Holding commits the remote lacks: publication pushes them along, so they count.
    git.update_base("main")
    git_command(root, "fast-import", "--quiet", input=(
        "commit refs/heads/main\ncommitter T <t@example.com> 1792483200 +0000\ndata 5\nlocal\n"
        f"from {git.head_sha()}\nM 100644 inline tests/acceptance/local_helper.py\ndata 10\nVALUE = 2\n\n\ndone\n"))
    assert check(case, {GOOD: "from local_helper import VALUE\n"}).commit == git_command(root, "rev-parse", "refs/heads/main")


def test_failed_refresh_is_unavailable_but_no_origin_uses_the_local_base(case):
    root, *_ = case
    git_command(root, "config", "remote.origin.url", str(root / "gone.git"))
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "VALUE = 1\n"})
    assert "unavailable" in str(refusal.value) and "origin" in str(refusal.value)
    git_command(root, "remote", "remove", "origin")
    assert check(case, {GOOD: "VALUE = 1\n"}).commit == git_command(root, "rev-parse", "refs/heads/main")


def test_korean_refusals_use_the_catalogue(case):
    root, config, git, history, scratch = case
    with pytest.raises(order.PlanError, match="수집 검사를 할 수 없어서"):
        check((root, replace(config, language="ko"), git, history, scratch), {GOOD: "V = 1\n"}, command="npm test")


def _move_helper_away_before_the_lock(git, git_history, monkeypatch):
    refreshes, update_base, real_lock = [], git.update_base, git.publish_lock
    monkeypatch.setattr(git, "update_base", lambda branch: (refreshes.append(branch), update_base(branch))[1])

    def lock():
        git_history.update({HELPER: None})
        return real_lock()

    monkeypatch.setattr(git, "publish_lock", lock)
    return refreshes


def test_base_move_before_the_lock_leaves_main_index_and_staged_files_alone(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, calls, _ = _publish_case(offline_publication, monkeypatch)
    _helper_on_main(git_history, git)
    # A staged file with the bytes a base update could add must survive any refusal.
    (root / "staged.txt").write_text("mine\n")
    git_command(root, "update-index", "--add", "staged.txt")
    before = _checkout(root, git)
    refreshes = _move_helper_away_before_the_lock(git, git_history, monkeypatch)
    labels = []
    monkeypatch.setattr(tracker, "ensure_labels", lambda specs: labels.append(specs))
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        order.publish(plan_with({GOOD: IMPORTS}), root, config, tracker, repo="owner/repo", git=git)
    assert labels == [] and _writes(calls) == [] and refreshes == []
    assert not (root / GOOD).exists() and tracker.list_issues() == []
    assert _checkout(root, git) == before


def test_base_move_before_the_lock_leaves_the_checkout_alone_in_flush(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, calls, _ = _publish_case(offline_publication, monkeypatch)
    _helper_on_main(git_history, git)
    order.publish(plan_with({GOOD: IMPORTS}), root, config, tracker, repo="owner/repo", git=git, defer="b")
    before = _checkout(root, git)
    refreshes = _move_helper_away_before_the_lock(git, git_history, monkeypatch)
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        order_flush.flush(root, config, tracker, git, StateStore.for_repo("owner/repo"), **FLUSH)
    assert _checkout(root, git) == before and refreshes == [] and _writes(calls) == []


def _move_after_the_check(git, git_history, monkeypatch):
    """A move once the base is collected: the checkout must land on the checked commit, not this one."""
    land = order_collect.land

    def moved(git_, branch, commit):
        git_history.update({HELPER: None})
        return land(git_, branch, commit)

    monkeypatch.setattr(order_collect, "land", moved)


def test_publication_lands_on_the_checked_commit_and_the_push_refuses_a_later_move(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, calls, origin = _publish_case(offline_publication, monkeypatch)
    # The checkout is behind: the check takes the commit with the helper, then the helper leaves main.
    git_history.update({HELPER: "VALUE = 1\n"})
    checked = git_history.tip
    _move_after_the_check(git, git_history, monkeypatch)
    with pytest.raises(GitError):
        order.publish(plan_with({GOOD: IMPORTS}), root, config, tracker, repo="owner/repo", git=git)
    assert (root / HELPER).exists(), "the checkout moved past the commit that was checked"
    assert git.is_ancestor(checked, "HEAD")
    assert git_command(origin, "rev-parse", "refs/heads/main") == git_history.tip
    assert GOOD not in git_command(origin, "ls-tree", "-r", "--name-only", "refs/heads/main")


def test_a_stale_symlink_the_refresh_would_cure_cannot_skip_a_retained_order(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    """The local link fails plan validation, so the order used to go uncollected until after the move."""
    root, config, tracker, git, calls, _ = _publish_case(offline_publication, monkeypatch)
    _helper_on_main(git_history, git)
    order.publish(plan_with({GOOD: IMPORTS}), root, config, tracker, repo="owner/repo", git=git, defer="b")
    git_history.update({HELPER: None})
    (root / GOOD).symlink_to("/nonexistent")
    before = _checkout(root, git)
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        order_flush.flush(root, config, tracker, git, StateStore.for_repo("owner/repo"), **FLUSH)
    assert _checkout(root, git) == before and _writes(calls) == []


def test_a_new_lanes_files_are_part_of_what_the_commit_may_change(  # noqa: F811
        offline_publication, monkeypatch):
    root, config, tracker, git, calls, origin = _publish_case(offline_publication, monkeypatch)
    plan = plan_with({GOOD: "VALUE = 1\n"})
    plan["new_lanes"] = [{"name": "extra", "paths": ["extra/**"], "skills": []}]
    (root / "extra").mkdir()
    order.publish(plan, root, config, tracker, repo="owner/repo", git=git)
    assert GOOD in git_command(origin, "ls-tree", "-r", "--name-only", "refs/heads/main")


def test_a_move_before_the_lock_that_still_collects_lands_and_publishes(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    real_lock = git.publish_lock
    monkeypatch.setattr(git, "publish_lock", lambda: (git_history.update({"moved.txt": "x\n"}), real_lock())[1])
    order.publish(plan_with({GOOD: "VALUE = 1\n"}), root, config, tracker, repo="owner/repo", git=git)
    assert (root / "moved.txt").exists()
    assert git_command(git_history.origin, "show", f"main:{GOOD}") == "VALUE = 1"


def test_unchanged_base_collects_once_per_publication(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    runs, real = [], processes.run_logged
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: (runs.append(a), real(*a, **k))[1])
    order.publish(plan_with({GOOD: "VALUE = 1\n"}), root, config, tracker, repo="owner/repo", git=git)
    assert len(runs) == 1


def test_unchanged_base_collects_once_per_flushed_order(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    order.publish(plan_with({GOOD: "VALUE = 1\n"}), root, config, tracker, repo="owner/repo", git=git, defer="b")
    runs, real = [], processes.run_logged
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: (runs.append(a), real(*a, **k))[1])
    result = order_flush.flush(root, config, tracker, git, StateStore.for_repo("owner/repo"), **FLUSH)
    assert result["status"] == "published" and len(runs) == 1


def test_a_flushed_batch_shares_one_fetched_base(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    for key in ("first", "second"):
        files = {f"tests/acceptance/test_{key}.py": "VALUE = 1\n"}
        order.publish(plan_with(files, key=key), root, config, tracker, repo="owner/repo", git=git, defer="b")
    fetches, fetch = [], git.fetch
    monkeypatch.setattr(git, "fetch", lambda *args: (fetches.append(args), fetch(*args))[1])
    order_flush.flush(root, config, tracker, git, StateStore.for_repo("owner/repo"), **FLUSH)
    assert fetches == [("origin", "main")]


def test_a_publication_fetches_once_before_the_lock(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    fetches, fetch = [], git.fetch
    monkeypatch.setattr(git, "fetch", lambda *args: (fetches.append(args), fetch(*args))[1])
    order.publish(plan_with({GOOD: "VALUE = 1\n"}), root, config, tracker, repo="owner/repo", git=git, dry_run=True)
    assert fetches == [("origin", "main")]


def test_flush_collects_each_order_alone_with_its_retained_files(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, calls, _ = _publish_case(offline_publication, monkeypatch)
    # The helper reaches main only through the other deferred order.
    _helper_on_main(git_history, git)
    order.publish(plan_with({GOOD: IMPORTS}, key="user"), root, config, tracker, repo="owner/repo", git=git, defer="b")
    git_history.update({HELPER: None})
    git.update_base("main")
    provider = plan_with({HELPER: "VALUE = 1\n"}, key="provider")
    order.publish(provider, root, config, tracker, repo="owner/repo", git=git, defer="b")
    with pytest.raises(order.PlanError) as refusal:
        order_flush.flush(root, config, tracker, git, StateStore.for_repo("owner/repo"), **FLUSH)
    assert GOOD in str(refusal.value) and "ModuleNotFoundError" in str(refusal.value)
    assert _writes(calls) == [] and tracker.list_issues() == []


def test_preread_refusal_starts_no_reader_and_records_nothing(case, monkeypatch):
    root, config, git, *_ = case
    monkeypatch.setattr(order, "executor_for", lambda name: pytest.fail("a refused preread starts no model"))
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        order.preread(plan_with({GOOD: IMPORTS}), root, config, lambda: FakeTracker(), repo="owner/repo", git=git)
    assert not Path(root / ".foragent/preread.json").exists()


# --- Tests that import a changed file (#1774) ---------------------------------------------------
SHARED = "tests/acceptance/test_shared.py"
SHARED_OK = "NAMES = ('a', 'b')\n\n\ndef case():\n    return list(NAMES)\n"
USER = "tests/test_user.py"
USER_TEXT = (
    "import sys, pathlib\n\nsys.path.insert(0, str(pathlib.Path(__file__).parent / 'acceptance'))\n"
    "from test_shared import NAMES, case\n\n\n"
    "class TestNames:\n    def test_two(self):\n        assert len(NAMES) == 2\n\n\n"
    "def test_case():\n    assert case() == ['a', 'b']\n"
)
RED_USER = "tests/test_red_user.py"
RED_USER_TEXT = (
    "import sys, pathlib\n\nsys.path.insert(0, str(pathlib.Path(__file__).parent / 'acceptance'))\n"
    "from test_shared import NAMES\n\n\ndef test_red():\n    assert len(NAMES) == 99\n"
)


@pytest.fixture
def shared_case(case):
    _, _, git, history, _ = case
    history.update({SHARED: SHARED_OK, USER: USER_TEXT, RED_USER: RED_USER_TEXT,
                    "tests/test_unrelated.py": "raise RuntimeError('only importers run')\n"})
    git.update_base("main")
    return case


def _runs(monkeypatch):
    calls = []
    real = processes.run_logged

    def counted(argv, **kwargs):
        calls.append(argv[-1])
        return real(argv, **kwargs)

    monkeypatch.setattr(processes, "run_logged", counted)
    return calls


def test_a_change_that_breaks_a_passing_importer_names_each_broken_test(shared_case, monkeypatch):
    calls = _runs(monkeypatch)
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: SHARED_OK.replace("('a', 'b')", "('a', 'b', 'c')")})
    message = str(refusal.value)
    assert f"{USER}::TestNames::test_two" in message and f"{USER}::test_case" in message, message
    assert RED_USER not in message.split("Broken tests:")[1], "A test red on the base is not a reason"
    assert "assert 3 == 2" in message, "The refusal shows why the test broke"
    # Collection, then the importers once on the base and once with the order's files, nothing else.
    assert len(calls) == 3 and all(USER in call and RED_USER in call for call in calls[1:])
    assert "test_unrelated" not in "".join(calls)


def test_an_importer_that_no_longer_imports_is_named_by_file_with_the_cause(shared_case):
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: "NAMES = ('a', 'b')\n"})
    broken = str(refusal.value).split("Broken tests:")[1]
    assert f"{USER}:\n" in broken and "case" in broken and "ImportError" in broken, broken
    assert RED_USER not in broken


def test_importers_that_keep_passing_publish_and_new_red_locks_are_not_run(shared_case):
    extended = SHARED_OK + "\n\ndef test_new_lock():\n    assert False, 'red until implemented'\n"
    assert check(shared_case, {SHARED: extended}) is not None


@pytest.mark.parametrize("files", [{SHARED: SHARED_OK}, {GOOD: "def test_alone():\n    pass\n"}])
def test_unchanged_files_or_files_nobody_imports_run_nothing_more(shared_case, monkeypatch, files):
    calls = _runs(monkeypatch)
    assert check(shared_case, files) is not None
    assert len(calls) == 1, "Only collection ran"


def test_the_orders_own_files_are_never_importers(shared_case, monkeypatch):
    _, _, git, history, _ = shared_case
    peer = "tests/acceptance/test_peer.py"
    peer_text = "from test_shared import NAMES\n\n\ndef test_peer():\n    assert len(NAMES) == 2\n"
    history.update({peer: peer_text})
    git.update_base("main")
    calls = _runs(monkeypatch)
    renamed = SHARED_OK.replace("('a', 'b')", "('x',)")
    # The order rewrites a lock that imports the shared file too: its own lock, red or not, is not checked.
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: renamed, peer: peer_text + "\n"})
    assert len(calls) == 3 and all(peer not in call for call in calls[1:])
    broken = str(refusal.value).split("Broken tests:")[1]
    assert USER in broken and peer not in broken


def test_an_importer_run_that_does_not_finish_fails_closed_without_a_slot(shared_case, monkeypatch):
    real, seen = processes.run_logged, []

    def importers_hang(argv, *, timeout_s, **kwargs):
        if "--collect-only" in argv[-1]:
            return real(argv, timeout_s=timeout_s, **kwargs)
        seen.append(timeout_s)
        return None, True, ""

    monkeypatch.setattr(processes, "run_logged", importers_hang)
    held = capacity._take_test_slot(1, wait=False)
    assert held is not None
    try:
        with pytest.raises(order.PlanError) as refusal:
            check(shared_case, {SHARED: SHARED_OK.replace("'b'", "'c'")})
    finally:
        held.close()
    message = str(refusal.value)
    assert "could not be checked" in message and "120 seconds" in message and USER in message, message
    assert seen and 0 < seen[0] <= order_collect.IMPORTERS_LIMIT_S


def test_an_unreadable_importer_report_fails_closed(shared_case, monkeypatch):
    # A real result object that says only "this run did not finish": the refusal must come from that.
    monkeypatch.setattr(order_collect, "_results",
                        lambda path, tree: order_collect._Run(False, {}, set(), {}, {}))
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: SHARED_OK.replace("'b'", "'c'")})
    message = str(refusal.value)
    assert "could not be checked" in message and "did not finish on the base" in message, message
    assert "TypeError" not in message, message


@pytest.mark.parametrize("name,text", [
    ("tests/test_package_form.py", "from tests.acceptance import test_shared\n\n\n"
     "def test_names():\n    assert test_shared.NAMES == ('a', 'b')\n"),
    ("tests/acceptance/test_relative.py", "from . import test_shared\n\n\n"
     "def test_names():\n    assert test_shared.NAMES == ('a', 'b')\n"),
    ("tests/acceptance/test_relative_from.py", "from .test_shared import (\n    NAMES,\n)\n\n\n"
     "def test_names():\n    assert NAMES == ('a', 'b')\n"),
    ("tests/test_multiline.py", "import os, \\\n    tests.acceptance.test_shared as shared\n\n\n"
     "def test_names():\n    assert shared.NAMES == ('a', 'b')\n"),
    ("tests/check_named_by_project.py", "from tests.acceptance.test_shared import NAMES\n\n\n"
     "def test_names():\n    assert NAMES == ('a', 'b')\n"),
    ("tests/test_한글.py", "from tests.acceptance.test_shared import NAMES\n\n\n"
     "def test_names():\n    assert NAMES == ('a', 'b')\n"),
])
def test_every_direct_import_form_is_an_importer(shared_case, name, text):
    """Package, relative, multi-line, by-name imports and files the project names otherwise are all found."""
    _, _, git, history, _ = shared_case
    package = {"tests/__init__.py": "", "tests/acceptance/__init__.py": ""}
    history.update({**package, name: text})
    git.update_base("main")
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: SHARED_OK.replace("('a', 'b')", "('a', 'z')")})
    assert f"{name}::test_names" in str(refusal.value).split("Broken tests:")[1], str(refusal.value)


def test_a_module_that_only_mentions_the_name_is_not_run(shared_case, monkeypatch):
    _, _, git, history, _ = shared_case
    history.update({"tests/test_mentions.py": "# test_shared is documented elsewhere\nWORD = 'test_shared'\n\n\n"
                    "def test_alone():\n    pass\n"})
    git.update_base("main")
    calls = _runs(monkeypatch)
    with pytest.raises(order.PlanError):
        check(shared_case, {SHARED: SHARED_OK.replace("'b'", "'c'")})
    assert len(calls) == 3 and all(USER in call and "test_mentions" not in call for call in calls[1:])


def test_a_changed_conftest_finds_the_tests_that_import_it_by_name(case, monkeypatch):
    """A changed conftest.py is a module like any other to a test that imports it by name."""
    _, _, git, history, _ = case
    conftest = "tests/acceptance/conftest.py"
    history.update({conftest: "def case():\n    return 1\n",
                    "tests/test_case_user.py": "import sys, pathlib\n\n"
                    "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'acceptance'))\n"
                    "from conftest import case\n\n\ndef test_case():\n    assert case() == 1\n"})
    git.update_base("main")
    calls = _runs(monkeypatch)
    with pytest.raises(order.PlanError) as refusal:
        check(case, {conftest: "def case():\n    return 2\n"})
    broken = str(refusal.value).split("Broken tests:")[1]
    assert "tests/test_case_user.py::test_case" in broken and "assert 2 == 1" in broken, broken
    assert len(calls) == 3


def test_a_changed_package_init_finds_the_importers_of_the_package(shared_case):
    _, _, git, history, _ = shared_case
    package = "tests/acceptance/shared/__init__.py"
    history.update({"tests/__init__.py": "", "tests/acceptance/__init__.py": "", package: "VALUE = 1\n",
                    "tests/test_package_user.py": "from tests.acceptance.shared import VALUE\n\n\n"
                    "def test_value():\n    assert VALUE == 1\n"})
    git.update_base("main")
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {package: "VALUE = 2\n"})
    assert "tests/test_package_user.py::test_value" in str(refusal.value).split("Broken tests:")[1]


PARAMETRISED = (
    "import sys, pathlib\n\nsys.path.insert(0, str(pathlib.Path(__file__).parent / 'acceptance'))\n"
    "import pytest\nfrom test_shared import LABEL, VALUE, NAMES\n\n\n"
    "@pytest.mark.parametrize('value', [VALUE], ids=[LABEL])\n"
    "def test_value(value):\n    assert value == 1\n\n\n"
    "@pytest.mark.parametrize('name', NAMES)\n"
    "def test_name(name):\n    assert name in ('a', 'b')\n"
)


@pytest.fixture
def parametrised_case(shared_case):
    _, _, git, history, _ = shared_case
    history.update({SHARED: "LABEL = 'old'\nVALUE = 1\n" + SHARED_OK, "tests/test_params.py": PARAMETRISED})
    git.update_base("main")
    return shared_case


def test_a_changed_parameter_label_is_still_the_same_test(parametrised_case):
    with pytest.raises(order.PlanError) as refusal:
        check(parametrised_case, {SHARED: "LABEL = 'new'\nVALUE = 2\n" + SHARED_OK})
    broken = str(refusal.value).split("Broken tests:")[1]
    assert "tests/test_params.py::test_value[new]" in broken and "assert 2 == 1" in broken, broken


def test_a_changed_parameter_label_alone_breaks_nothing(parametrised_case):
    assert check(parametrised_case, {SHARED: "LABEL = 'new'\nVALUE = 1\n" + SHARED_OK}) is not None


def test_a_new_failing_parameter_of_a_passing_test_is_broken(parametrised_case):
    with pytest.raises(order.PlanError) as refusal:
        check(parametrised_case, {SHARED: "LABEL = 'old'\nVALUE = 1\n" + SHARED_OK.replace("('a', 'b')", "('a', 'b', 'c')")})
    assert "tests/test_params.py::test_name[c]" in str(refusal.value).split("Broken tests:")[1]


def test_a_passing_parameter_that_no_longer_runs_is_broken(parametrised_case):
    with pytest.raises(order.PlanError) as refusal:
        check(parametrised_case, {SHARED: "LABEL = 'old'\nVALUE = 1\n" + SHARED_OK.replace("('a', 'b')", "('a',)")})
    broken = str(refusal.value).split("Broken tests:")[1]
    assert "tests/test_params.py::test_name[b]" in broken and "no longer runs" in broken, broken
    assert "test_name[a]" not in broken


@pytest.mark.parametrize("stop", [
    "raise KeyboardInterrupt\n",
    "import pytest\npytest.exit('stopped by the order', returncode=0)\n",
    "import os\nos._exit(0)\n",
])
def test_an_order_that_stops_the_importer_run_is_refused(shared_case, stop):
    """An incomplete run with the order's files is not a pass, whatever its exit code."""
    with pytest.raises(order.PlanError) as refusal:
        # Collecting the shared file alone passes; only the importer loading it stops the run.
        check(shared_case, {SHARED: SHARED_OK + "\nimport sys\nif {'test_user', 'test_red_user'} & set(sys.modules):\n"
                            + "".join(f"    {line}\n" for line in stop.splitlines())})
    broken = str(refusal.value).split("Broken tests:")[1]
    # Named by test when the run stopped, or by file when pytest reported the stop as that file's error.
    assert f"{USER}::test_case" in broken or f"{USER}:\n" in broken, broken
    assert "stopped" in broken, broken
    assert RED_USER not in broken


def test_a_stop_inside_one_test_names_the_tests_it_kept_from_running(shared_case):
    _, _, git, history, _ = shared_case
    history.update({USER: USER_TEXT + "\n\ndef test_zz_after():\n    pass\n"})
    git.update_base("main")
    stops = SHARED_OK.replace("    return list(NAMES)", "    raise KeyboardInterrupt")
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: stops})
    broken = str(refusal.value).split("Broken tests:")[1]
    assert f"{USER}::test_zz_after" in broken and "stopped" in broken, broken


def test_a_base_run_that_does_not_finish_is_unavailable_not_a_pass(shared_case):
    _, _, git, history, _ = shared_case
    history.update({"tests/test_stops_on_base.py": "from test_shared import NAMES\n\nraise KeyboardInterrupt\n"})
    git.update_base("main")
    with pytest.raises(order.PlanError) as refusal:
        check(shared_case, {SHARED: SHARED_OK.replace("'b'", "'c'")})
    assert "could not be checked" in str(refusal.value) and "did not finish on the base" in str(refusal.value)


def test_korean_importer_refusal_uses_the_catalogue(shared_case):
    root, config, git, _, _ = shared_case
    config = replace(config, language="ko")
    plan = plan_with({SHARED: "NAMES = ()\n"})
    with pytest.raises(order.PlanError) as refusal:
        order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git)
    assert "깨진 시험" in str(refusal.value) and USER in str(refusal.value)
