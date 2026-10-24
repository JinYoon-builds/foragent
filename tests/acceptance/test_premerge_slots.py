"""Acceptance test for premerge-slots. Locked by Foragent; do not edit."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
import os
from pathlib import Path
import shlex
from threading import Event
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from conftest import git_command
from foragent import capacity, executors, orch, runner
from foragent.state import StateStore
from test_baseline_cache import another_repository, assert_cache_written
from test_orch_review_handoff import make_case
from test_premerge_baseline import (
    ADDED, EXISTING, NEW_FAILURE, PENDING, baseline_premerge, premerge_report,
    update_pr_head,
)
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


@pytest.fixture
def slotted_premerge(baseline_premerge, isolated_home, monkeypatch):
    """Reuse D53's real suites, with isolated machine state and fake services."""
    case, main, pr, head = baseline_premerge

    def forbidden(*args, **kwargs):
        raise AssertionError("Premerge acceptance tests must not signal processes or run real models")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    machine = isolated_home / ".local/share/foragent"
    monkeypatch.setattr(capacity, "machine_root", lambda: machine)
    monkeypatch.delenv(capacity.TEST_SLOT_ENV, raising=False)
    case.ctx.config = replace(case.ctx.config, test_slots=1)
    case.store = case.ctx.store = StateStore(machine / "premerge", now=lambda: case.clock[0])
    # A PR need not have a runner record: waiting belongs to the CLI report.
    assert case.store.get_run(pr) is None
    yield case, main, pr, head
    assert case.tracker.merged_prs == []
    assert case.tracker.list_comments(pr) == []
    assert not case.implementer.calls and not case.reviewer.calls


class SuiteProcesses:
    """Observe real pytest processes; pause their boundary without replacing slots."""

    def __init__(self, case, monkeypatch, *, block=None):
        self.calls = []
        self.started, self.entered, self.release = Event(), Event(), Event()
        original = orch.run_logged

        def process(argv, *, cwd, **kwargs):
            words = shlex.split(argv[2])
            if "pytest" not in words:
                return original(argv, cwd=cwd, **kwargs)
            sha = git_command(Path(cwd), "rev-parse", "HEAD")
            side = "base" if sha == case.history.tip else "head"
            call = SimpleNamespace(side=side, words=words, executed=set())
            self.calls.append(call)
            self.started.set()
            if side == block:
                self.entered.set()
                assert self.release.wait(15), "Test did not release the paused suite process"
            result = original(argv, cwd=cwd, **kwargs)
            for index, word in enumerate(words):
                if word.startswith(("--junitxml=", "--junit-xml=")):
                    report = Path(word.split("=", 1)[1])
                elif word in {"--junitxml", "--junit-xml"}:
                    report = Path(words[index + 1])
                else:
                    continue
                if not report.is_absolute():
                    report = Path(cwd) / report
                call.executed = {runner.junit_node_id(test)
                                 for test in ET.parse(report).iter("testcase")}
                break
            return result

        monkeypatch.setattr(orch, "run_logged", process)
        monkeypatch.setattr(runner, "run_logged", process)


def assert_verdict(result, report, *, broken=()):
    assert result == int(bool(broken)), report
    assert report["tests"] == ("fail" if broken else "pass"), report
    assert report["ready"] is (not broken), report
    assert report["broken"] == list(broken), report
    assert report["failing_on_base"] == [PENDING], report


@pytest.mark.parametrize("side", ["head", "base"])
def test_premerge_slots_hold_each_uncached_suite_until_completion(
    slotted_premerge, monkeypatch, capsys, side,
):
    case, main, pr, _ = slotted_premerge
    checks = SuiteProcesses(case, monkeypatch, block=side)
    attempted, acquired = Event(), Event()

    def competitor():
        with capacity.hold_test_slot(1, waiting=attempted.set):
            acquired.set()
            attempted.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        premerge = pool.submit(premerge_report, main, pr, capsys)
        try:
            assert checks.entered.wait(10), f"Uncached premerge never ran its {side} suite"
            contender = pool.submit(competitor)
            assert attempted.wait(5), "Competitor never attempted the machine-wide slot"
            overlapped = acquired.is_set()
        finally:
            checks.release.set()
        result, report = premerge.result(timeout=20)
        contender.result(timeout=10)

    assert_verdict(result, report)
    assert {call.side for call in checks.calls} == {"head", "base"}, (
        "Without a cache, premerge must still run both sides"
    )
    assert acquired.is_set(), "Premerge retained its test slot after its suite completed"
    assert not overlapped, (
        f"The {side} premerge suite did not hold the machine-wide test slot for its duration"
    )


def test_premerge_slots_wait_for_occupied_slot_then_run_and_report_wait(
    slotted_premerge, monkeypatch, capsys,
):
    case, main, pr, _ = slotted_premerge
    checks = SuiteProcesses(case, monkeypatch)
    reached = Event()
    hold_slot = capacity.hold_test_slot

    @contextmanager
    def observed_slot(ceiling, *, waiting=None):
        def observe_wait():
            reached.set()
            if waiting is not None:
                waiting()

        with hold_slot(ceiling, waiting=observe_wait):
            yield

    monkeypatch.setattr(capacity, "hold_test_slot", observed_slot)

    def invoke():
        try:
            return premerge_report(main, pr, capsys)
        finally:
            reached.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with hold_slot(1):
            premerge = pool.submit(invoke)
            assert reached.wait(10), "Premerge neither queued for a slot nor returned"
            # A real occupied interval, plus the CLI fixture's injected clock.
            Event().wait(0.25)
            case.clock[0] += timedelta(seconds=3)
            started_early = checks.started.is_set()
            finished_early = premerge.done()
        result, report = premerge.result(timeout=20)

    assert_verdict(result, report)
    assert checks.started.is_set(), "Premerge did not run its suite after the slot was released"
    assert not started_early and not finished_early, (
        "Premerge started a suite while the only machine-wide test slot was held"
    )
    waited = report.get("waited_seconds")
    assert type(waited) in (int, float) and waited > 0, (
        f"Premerge JSON must report positive slot waited_seconds; got {waited!r}"
    )


def warm_runner_baseline(case, tmp_path, monkeypatch, isolated_home):
    """Populate #257 through run_issue, rather than inventing a cache payload."""
    history = another_repository(case.history, tmp_path / "baseline-repository")
    directory = tmp_path / "baseline-runner"
    directory.mkdir()
    warmer = make_case(history, directory, monkeypatch, skill_libraries=[])
    approval = asdict(warmer.store.load_approval(1))
    warmer.store = warmer.ctx.store = StateStore(
        case.store.root.parent / "baseline-runner", now=lambda: case.clock[0],
    )
    warmer.store.save_approval(**approval)
    warmer.store.start_run(1, branch="foragent/issue-1", worktree=warmer.store.worktree_path(1))
    warmer.ctx.config = replace(warmer.config, test_command=case.ctx.config.test_command, test_slots=1)
    assert runner.run_issue(1, warmer.ctx) == "merged", warmer.store.log_path(1).read_text()
    assert_cache_written(isolated_home)
    assert case.history.tip == git_command(history.clone, "rev-parse", "origin/main")


@pytest.mark.parametrize("scenario,broken", [
    pytest.param("preexisting-failure", (), id="preexisting-failure"),
    pytest.param("regression", (EXISTING,), id="regression"),
    pytest.param("new-failure", (NEW_FAILURE,), id="new-failure"),
])
def test_premerge_slots_reuse_runner_cached_base_and_preserve_verdict(
    slotted_premerge, tmp_path, monkeypatch, isolated_home, capsys, scenario, broken,
):
    case, main, pr, head = slotted_premerge
    checks = SuiteProcesses(case, monkeypatch)
    warm_runner_baseline(case, tmp_path, monkeypatch, isolated_home)
    assert any(call.side == "base" and PENDING in call.executed for call in checks.calls), (
        "The runner did not execute the acceptance baseline on the PR's base SHA"
    )
    if scenario == "regression":
        update_pr_head(case, pr, head, {"existing-feature.txt": "broken\n"})
    elif scenario == "new-failure":
        update_pr_head(case, pr, head, {
            ADDED: (
                '"""Acceptance test for added-by-pr. Locked by Foragent; do not edit."""\n'
                "def test_new_feature():\n    assert False, 'new PR regression'\n"
            ),
        })
    checks.calls.clear()

    result, report = premerge_report(main, pr, capsys)

    assert_verdict(result, report, broken=broken)
    assert any(call.side == "head" and PENDING in call.executed for call in checks.calls), (
        "A cached base must not skip the PR head's acceptance tests"
    )
    # A base collection-only probe or unit-only run is allowed: #257 caches
    # acceptance passing IDs, not the verdict of every possible test suite.
    base_acceptance_runs = [call.words for call in checks.calls if call.side == "base"
                            and not {"--collect-only", "--co"}.intersection(call.words)
                            and any(node and node.startswith(case.ctx.config.acceptance_dir)
                                    for node in call.executed)]
    assert base_acceptance_runs == [], (
        "Premerge reran base acceptance tests despite a matching runner baseline cache: "
        f"{base_acceptance_runs}"
    )


# #331 extends D81 to premerge: CI owns units; local checks compare acceptance.
UNIT_SUITE = "tests/test_premerge_unit.py"
UNIT_ID = f"{UNIT_SUITE}::test_unit"
QUEUED_SUITE = "tests/acceptance/test_queued_feature.py"
QUEUED_ID = f"{QUEUED_SUITE}::test_queued_feature"


def configure_ci_premerge(slotted_premerge, *, workflow, setting="auto", files=None):
    """Use real PR/base suites, including a unit regression that CI must own."""
    case, main, pr, head = slotted_premerge
    case.history.update({
        ".github/workflows/ci.yml": workflow,
        UNIT_SUITE: (
            "from pathlib import Path\n\n"
            "def test_unit():\n"
            "    assert Path('feature.txt').read_text() == 'old', 'unit regression belongs to CI'\n"
        ),
        **(files or {}),
    })
    case.git.fetch()
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.ctx.config = replace(case.ctx.config, runner_unit_tests=setting)
    return case, main, pr, head


def suite_executions(checks, side):
    # Collection probes are allowed, but must never disguise a full-suite run.
    return [call for call in checks.calls if call.side == side
            and not {"--collect-only", "--co"}.intersection(call.words)]


def assert_ci_acceptance_only(case, checks, *, cached):
    unit_runs = [call.words for call in checks.calls if UNIT_ID in call.executed]
    assert unit_runs == [], (
        "Trusted PR CI must own the unit suite; premerge launched local unit pytest: "
        f"{unit_runs}"
    )
    head_runs = suite_executions(checks, "head")
    head_ids = {node for call in head_runs for node in call.executed}
    assert {EXISTING, PENDING} <= head_ids, (
        f"Premerge must still execute head acceptance with trusted CI: {head_runs}"
    )
    assert all(node.startswith(case.ctx.config.acceptance_dir) for node in head_ids), head_ids
    base_runs = suite_executions(checks, "base")
    if cached:
        assert base_runs == [], (
            "Trusted PR CI premerge reran the cached base instead of comparing acceptance IDs: "
            f"{[call.words for call in base_runs]}"
        )
    else:
        base_ids = {node for call in base_runs for node in call.executed}
        assert {EXISTING, PENDING} <= base_ids, "A missing acceptance baseline must be built"
        assert all(node.startswith(case.ctx.config.acceptance_dir) for node in base_ids), base_ids


@pytest.mark.parametrize("setting", ["auto", "ci"])
def test_premerge_slots_trusted_ci_runs_acceptance_and_caches_missing_base(
    slotted_premerge, monkeypatch, isolated_home, capsys, setting,
):
    from test_ci_trusted_checks import PR_WORKFLOW

    case, main, pr, _ = configure_ci_premerge(
        slotted_premerge, workflow=PR_WORKFLOW, setting=setting,
    )
    checks = SuiteProcesses(case, monkeypatch)
    assert not list((case.store.root.parent / "baselines").glob("*.json"))

    result, report = premerge_report(main, pr, capsys)

    assert_ci_acceptance_only(case, checks, cached=False)
    assert_verdict(result, report)
    assert_cache_written(isolated_home)
    checks.calls.clear()

    result, report = premerge_report(main, pr, capsys)

    assert_ci_acceptance_only(case, checks, cached=True)
    assert_verdict(result, report)


@pytest.mark.parametrize("scenario,broken,ci,conflicts", [
    pytest.param("preexisting-failure", (), "pass", [], id="preexisting-failure"),
    pytest.param("regression", (EXISTING,), "pass", [], id="regression"),
    pytest.param("new-failure", (NEW_FAILURE,), "pass", [], id="new-failure"),
    pytest.param("red-ci", (), "fail", [], id="red-ci"),
    pytest.param("conflict", (), "pass", ["feature.txt"], id="conflict"),
])
def test_premerge_slots_trusted_ci_reuses_runner_baseline_and_preserves_verdict(
    slotted_premerge, tmp_path, monkeypatch, isolated_home, capsys,
    scenario, broken, ci, conflicts,
):
    import json

    from test_ci_trusted_checks import PR_WORKFLOW

    case, main, pr, head = configure_ci_premerge(slotted_premerge, workflow=PR_WORKFLOW)
    if scenario == "conflict":
        case.history.update({"feature.txt": "competing base version\n"})
    case.tracker.set_pr_checks(pr, ci)
    checks = SuiteProcesses(case, monkeypatch)
    warm_runner_baseline(case, tmp_path, monkeypatch, isolated_home)
    assert any(call.side == "base" and EXISTING in call.executed for call in checks.calls)
    # The warmer uses the same issue branch in its independent repository.
    # Introduce PR regressions only after it has populated the healthy base.
    if scenario == "regression":
        update_pr_head(case, pr, head, {"existing-feature.txt": "broken\n"})
    elif scenario == "new-failure":
        update_pr_head(case, pr, head, {
            ADDED: "def test_new_feature():\n    assert False, 'new acceptance regression'\n",
        })
    checks.calls.clear()

    result = main(["orch", "premerge", str(pr), "--json"])
    report = json.loads(capsys.readouterr().out)

    assert_ci_acceptance_only(case, checks, cached=True)
    assert report["pr"] == pr and report["ci"] == ci, report
    assert report["tests"] == ("fail" if broken else "pass"), report
    assert report["broken"] == list(broken), report
    assert report["failing_on_base"] == [PENDING], report
    assert report["conflicts"] == conflicts, report
    ready = not broken and ci == "pass" and not conflicts
    assert report["ready"] is ready and result == int(not ready), report


@pytest.mark.parametrize("mode", ["no-workflow", "push-only", "all"])
def test_premerge_slots_switch_from_full_suite_to_trusted_ci(
    slotted_premerge, monkeypatch, capsys, mode,
):
    from test_ci_trusted_checks import PR_WORKFLOW, PUSH_ONLY_WORKFLOW

    workflow = {"no-workflow": None, "push-only": PUSH_ONLY_WORKFLOW, "all": PR_WORKFLOW}[mode]
    case, main, pr, _ = configure_ci_premerge(
        slotted_premerge, workflow=workflow, setting="all" if mode == "all" else "auto",
    )
    checks = SuiteProcesses(case, monkeypatch)

    result, report = premerge_report(main, pr, capsys)

    # Keep today's full-suite behaviour, including its unit-regression verdict.
    assert_verdict(result, report, broken=(UNIT_ID,))
    for side in ("head", "base"):
        assert any(UNIT_ID in call.executed for call in suite_executions(checks, side)), (
            f"Without trusted PR CI, {side} must retain the unit suite ({mode})"
        )

    # Enabling PR CI/the automatic setting must change what premerge executes.
    # Keeping the transition here makes each control a red test on today's main.
    case.history.update({".github/workflows/ci.yml": PR_WORKFLOW})
    case.git.fetch()
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.ctx.config = replace(case.ctx.config, runner_unit_tests="auto")
    checks.calls.clear()

    result, report = premerge_report(main, pr, capsys)

    assert_ci_acceptance_only(case, checks, cached=False)
    assert_verdict(result, report)


def test_premerge_slots_trusted_ci_excludes_locked_tests_of_approved_unmerged_issues(
    slotted_premerge, tmp_path, monkeypatch, isolated_home, capsys,
):
    from foragent.issue import body_hash
    from test_ci_trusted_checks import PR_WORKFLOW

    case, main, pr, _ = configure_ci_premerge(slotted_premerge, workflow=PR_WORKFLOW, files={
        QUEUED_SUITE: (
            '"""Acceptance test for queued-feature. Locked by Foragent; do not edit."""\n'
            "def test_queued_feature():\n    assert False, 'approved issue is not merged yet'\n"
        ),
    })
    body = (
        "<!-- foragent:v1 -->\n## Goal\nImplement the queued feature.\n"
        "## Out of scope\nOther features.\n## Done when\n"
        f"```sh\npytest {QUEUED_SUITE}\n```\n"
        f"[{QUEUED_SUITE}](https://github.com/owner/repo/blob/main/{QUEUED_SUITE})\n"
    )
    case.tracker.add_issue(100, "Queued feature", body, labels=["approved"])
    case.store.save_approval(
        100, body=body, body_sha=body_hash(body),
        acceptance_blobs=case.git.blob_shas("origin/main", [QUEUED_SUITE]),
        approved_by="human", approved_at=case.clock[0].isoformat(),
    )
    assert case.store.load_approval(100).acceptance_blobs.keys() == {QUEUED_SUITE}
    assert case.tracker.get_issue(100).state == "open"
    assert case.tracker.find_pr("foragent/issue-100") is None
    checks = SuiteProcesses(case, monkeypatch)
    warm_runner_baseline(case, tmp_path, monkeypatch, isolated_home)
    checks.calls.clear()

    result, report = premerge_report(main, pr, capsys)

    queued_runs = [call.words for call in checks.calls if QUEUED_ID in call.executed]
    assert queued_runs == [], (
        "Premerge executed a locked acceptance file owned by an approved, unmerged issue: "
        f"{queued_runs}"
    )
    assert_ci_acceptance_only(case, checks, cached=True)
    assert_verdict(result, report)
