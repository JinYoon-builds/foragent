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
