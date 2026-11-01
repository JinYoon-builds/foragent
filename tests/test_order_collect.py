"""The collection check's budget, cleanup, base choice and fail-closed causes (#1523)."""

from dataclasses import replace
from pathlib import Path
import sys
import tempfile

import pytest

from foragent import capacity, order, order_collect, order_flush, processes
from foragent.config import Config
from foragent.git import GitRepo
from foragent.state import StateStore
from foragent.tracker import FakeTracker

from conftest import git_command
from test_order import offline_publication  # noqa: F401


GOOD = "tests/acceptance/test_good.py"
HELPER = "tests/acceptance/collect_helper.py"
IMPORTS = "from collect_helper import VALUE\n\ndef test_value():\n    assert VALUE\n"


def plan_with(files, key="only", **extra):
    return {"id": f"plan-{key}", "title": "T", "summary": "S", "spec_markdown": "# S\n", "issues": [{
        "key": key, "title": "T", "lane": "main", "size": "s", "goal": "G", "out_of_scope": "O",
        "done_when": [f"uv run pytest -q {next(iter(files), GOOD)}"] if files else ["true"],
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


def check(case, files, **options):
    root, config, git, _, _ = case
    plan = plan_with(files)
    return order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git, **options)


def slot_is_free():
    stream = capacity._take_test_slot(1, wait=False)
    if stream is None:
        return False
    stream.close()
    return True


def test_a_plan_without_acceptance_files_touches_nothing(case, monkeypatch):
    root, config, *_ = case

    class NoGit:
        def __getattr__(self, name):
            pytest.fail(f"no base is needed without acceptance files: {name}")

    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("nothing to collect"))
    plan = plan_with({})
    assert order_collect.check(plan, order.validate_plan(plan, root, config), root, config, NoGit()) is None


def test_helper_without_test_functions_collects_and_leaves_nothing(case):
    root, config, git, history, scratch = case
    result = check(case, {HELPER: "VALUE = 1\n"})
    assert result.commit == git_command(root, "rev-parse", "refs/remotes/origin/main")
    assert list(scratch.iterdir()) == []
    assert slot_is_free()


def test_import_error_names_file_and_cause_then_cleans_up(case):
    *_, scratch = case
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: IMPORTS})
    assert GOOD in str(refusal.value) and "ModuleNotFoundError" in str(refusal.value)
    assert "collect_helper" in str(refusal.value) and str(scratch) not in str(refusal.value)
    assert list(scratch.iterdir()) == []
    assert slot_is_free()


def test_only_listed_files_join_the_base_and_no_caller_import_path_counts(case, monkeypatch, tmp_path):
    root, *_ = case
    (root / HELPER).parent.mkdir(parents=True, exist_ok=True)
    (root / HELPER).write_text("VALUE = 1\n")
    elsewhere = tmp_path / "caller-path"
    elsewhere.mkdir()
    (elsewhere / "collect_helper.py").write_text("VALUE = 1\n")
    monkeypatch.setenv("PYTHONPATH", str(elsewhere))
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        check(case, {GOOD: IMPORTS})
    assert check(case, {GOOD: IMPORTS, HELPER: "VALUE = 2\n"}) is not None


def test_a_script_or_unimported_helper_is_published_but_never_run(case, monkeypatch):
    root, config, git, *_ = case
    witness = root.parent / "ran"
    body = f"open({str(witness)!r}, 'w').close()\nraise SystemExit(1)\n"
    plan = plan_with({"tests/acceptance/check_script.py": body, HELPER: body})
    plan["issues"][0]["done_when"] = ["python tests/acceptance/check_script.py"]
    order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git)
    assert not witness.exists()


def test_a_test_exiting_at_import_is_refused_and_named(case):
    with pytest.raises(order.PlanError) as refusal:
        check(case, {GOOD: "raise SystemExit(1)\n"})
    assert GOOD in str(refusal.value) and "SystemExit" in str(refusal.value)
    assert "unavailable" not in str(refusal.value)


def test_a_custom_test_name_counts_when_done_when_hands_it_to_pytest(case):
    with pytest.raises(order.PlanError, match="collect_helper"):
        check(case, {"tests/acceptance/check_custom.py": IMPORTS})


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
    assert "ModuleNotFoundError" not in message
    assert 0 < calls[0][1] <= order_collect.LIMIT_S
    assert slot_is_free()
    calls.clear()
    earlier = order_collect.Collected("not-this-base", order_collect.LIMIT_S - 1)
    with pytest.raises(order.PlanError):
        check(case, {GOOD: "VALUE = 1\n"}, earlier=earlier)
    assert 0 < calls[0][1] <= 1


def test_spent_budget_starts_nothing(case, monkeypatch):
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("no budget is left"))
    earlier = order_collect.Collected("not-this-base", order_collect.LIMIT_S)
    with pytest.raises(order.PlanError, match="unavailable"):
        check(case, {GOOD: "VALUE = 1\n"}, earlier=earlier)


def test_slot_wait_shares_the_deadline_and_is_unavailable(case, monkeypatch):
    monkeypatch.setattr(order_collect, "LIMIT_S", 0.2)
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("no slot was admitted"))
    held = capacity._take_test_slot(1, wait=False)
    try:
        with pytest.raises(order.PlanError) as refusal:
            check(case, {GOOD: "VALUE = 1\n"})
    finally:
        held.close()
    assert "unavailable" in str(refusal.value) and "test slot" in str(refusal.value) and GOOD in str(refusal.value)
    assert slot_is_free()


@pytest.mark.parametrize("command,cause", [
    ("npm test", "not a pytest command"),
    ("/__missing__/pytest", "code 127"),
    # A launcher that exits 0 without a report established nothing.
    ("/usr/bin/true pytest", "no collection result"),
])
def test_commands_that_cannot_collect_are_unavailable(case, command, cause):
    root, config, git, history, scratch = case
    with pytest.raises(order.PlanError) as refusal:
        check((root, replace(config, test_command=command), git, history, scratch), {GOOD: "VALUE = 1\n"})
    assert "unavailable" in str(refusal.value) and cause in str(refusal.value)
    assert list(scratch.iterdir()) == []


def test_no_command_is_detected_from_the_clean_base_not_the_caller(case):
    root, config, git, history, scratch = case
    (root / "pyproject.toml").write_text("[project]\nname = 'caller-only'\n")
    assert "pyproject.toml" not in git_command(root, "ls-files")
    with pytest.raises(order.PlanError, match="no test command"):
        check((root, replace(config, test_command=None), git, history, scratch), {GOOD: "VALUE = 1\n"})


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
    assert seen[0].startswith("PYTEST_ADDOPTS= uv run pytest ") and "--collect-only" in seen[0]


def test_non_python_acceptance_files_have_nothing_to_collect(case, monkeypatch):
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("nothing to collect"))
    root, config, git, *_ = case
    plan = plan_with({"tests/acceptance/data.json": "{}\n"})
    assert order_collect.check(plan, order.validate_plan(plan, root, config), root, config, git) is not None


def test_same_base_is_not_collected_twice_and_a_new_one_is(case, monkeypatch):
    root, config, git, history, _ = case
    first = check(case, {GOOD: "VALUE = 1\n"})
    monkeypatch.setattr(processes, "run_logged", lambda *a, **k: pytest.fail("this base is already checked"))
    assert check(case, {GOOD: "VALUE = 1\n"}, refreshed=True, earlier=first) is first
    monkeypatch.undo()
    history.update({"unrelated.txt": "moved\n"})
    git.update_base("main")
    earlier = order_collect.Collected(first.commit, 5.0)
    second = check(case, {GOOD: "VALUE = 1\n"}, refreshed=True, earlier=earlier)
    assert second.commit == git.head_sha() != first.commit and second.spent > 5.0


def test_base_is_the_commit_main_would_receive(case):
    root, config, git, history, _ = case
    # Behind the remote: the remote decides.
    history.update({HELPER: "VALUE = 1\n"})
    assert (root / HELPER).exists() is False
    assert check(case, {GOOD: IMPORTS}).commit == git_command(history.origin, "rev-parse", "refs/heads/main")
    # Holding commits the remote lacks: publication pushes them along, so they count.
    git.update_base("main")
    history_head = git.head_sha()
    git_command(root, "fast-import", "--quiet", input=(
        "commit refs/heads/main\ncommitter T <t@example.com> 1792483200 +0000\ndata 5\nlocal\n"
        f"from {history_head}\nM 100644 inline tests/acceptance/local_helper.py\ndata 10\nVALUE = 2\n\n\ndone\n"))
    local = git_command(root, "rev-parse", "refs/heads/main")
    body = "from local_helper import VALUE\n"
    assert check(case, {GOOD: body}).commit == local


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
        check((root, replace(config, language="ko", test_command="npm test"), git, history, scratch), {GOOD: "V = 1\n"})


def _publish_case(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, calls, origin = offline_publication
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(order, "require_clean_preread", lambda *a, **k: None)
    return root, replace(config, language="en"), tracker, git, calls, origin


def test_base_move_during_publication_is_checked_before_any_write(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, calls, _ = _publish_case(offline_publication, monkeypatch)
    git_history.update({HELPER: "VALUE = 1\n"})
    git.update_base("main")
    update_base = git.update_base

    def moved_meanwhile(branch):
        git_history.update({HELPER: None})
        return update_base(branch)

    monkeypatch.setattr(git, "update_base", moved_meanwhile)
    labels = []
    monkeypatch.setattr(tracker, "ensure_labels", lambda specs: labels.append(specs))
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        order.publish(plan_with({GOOD: IMPORTS}), root, config, tracker, repo="owner/repo", git=git)
    assert labels == [] and not [args for args in calls if "commit" in args or args[:1] == ["push"]]
    assert not (root / GOOD).exists() and tracker.list_issues() == []


def test_unchanged_base_collects_once_per_publication(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    runs = []
    real = processes.run_logged

    def counted(*args, **kwargs):
        runs.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(processes, "run_logged", counted)
    order.publish(plan_with({GOOD: "VALUE = 1\n"}), root, config, tracker, repo="owner/repo", git=git)
    assert len(runs) == 1


def test_a_publication_fetches_once_before_the_lock(offline_publication, monkeypatch):  # noqa: F811
    root, config, tracker, git, *_ = _publish_case(offline_publication, monkeypatch)
    fetches = []
    fetch = git.fetch
    monkeypatch.setattr(git, "fetch", lambda *args: (fetches.append(args), fetch(*args))[1])
    order.publish(plan_with({GOOD: "VALUE = 1\n"}), root, config, tracker, repo="owner/repo", git=git, dry_run=True)
    assert fetches == [("origin", "main")]


def test_flush_collects_each_order_alone_with_its_retained_files(  # noqa: F811
        offline_publication, git_history, monkeypatch):
    root, config, tracker, git, calls, _ = _publish_case(offline_publication, monkeypatch)
    store = StateStore.for_repo("owner/repo")
    # The helper reaches main only through the other deferred order.
    provider = plan_with({HELPER: "VALUE = 1\n"}, key="provider")
    user = plan_with({GOOD: IMPORTS}, key="user")
    git_history.update({HELPER: "VALUE = 1\n"})
    git.update_base("main")
    order.publish(user, root, config, tracker, repo="owner/repo", git=git, defer="b")
    git_history.update({HELPER: None})
    git.update_base("main")
    order.publish(provider, root, config, tracker, repo="owner/repo", git=git, defer="b")
    with pytest.raises(order.PlanError) as refusal:
        order_flush.flush(root, config, tracker, git, store, repo="owner/repo", boundary="b")
    assert GOOD in str(refusal.value) and "ModuleNotFoundError" in str(refusal.value)
    assert not [args for args in calls if "commit" in args or args[:1] == ["push"]]
    assert tracker.list_issues() == []


def test_preread_refusal_starts_no_reader_and_records_nothing(case, monkeypatch):
    root, config, git, *_ = case
    monkeypatch.setattr(order, "executor_for", lambda name: pytest.fail("a refused preread starts no model"))
    with pytest.raises(order.PlanError, match="ModuleNotFoundError"):
        order.preread(plan_with({GOOD: IMPORTS}), root, config, lambda: FakeTracker(), repo="owner/repo", git=git)
    assert not Path(root / ".foragent/preread.json").exists()
