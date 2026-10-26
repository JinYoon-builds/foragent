from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import shlex
import sys
import time
import xml.etree.ElementTree as ET

import pytest

from foragent.config import Config
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.reasons import Reason, parse_reason
from foragent.runner import CheckResult, RunContext, run_issue
from foragent.state import StateStore
from foragent.tracker import FakeTracker

from conftest import ACCEPTANCE, BASE_TIME, git_command
from runner_support import BODY, enable_reviewer, run_case


@pytest.mark.parametrize("options", ["", "--rootdir=tests --junit-prefix=project"])
def test_baseline_preserves_exact_parameterized_ids_and_excludes_nonpasses(run_case, options):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command=f"{shlex.quote(sys.executable)} -m pytest -q {options}")
    c.history.update({"tests/acceptance/test_baseline_cases.py": '''import pytest

class TestGroup:
    @pytest.mark.parametrize("value", ["a b", "x&y"], ids=["a b", "x&y"])
    def test_pass(self, value):
        assert value

@pytest.mark.skip(reason="later")
def test_skipped():
    pass

@pytest.mark.xfail(reason="later")
def test_expected_failure():
    assert False

@pytest.fixture
def broken_teardown():
    yield
    assert False, "teardown failed"

def test_failed_teardown(broken_teardown):
    pass

def test_future():
    assert False, "another issue"
'''})
    assert run_issue(1, c.ctx) == "merged"
    prompt = c.executor.calls[0]["prompt"]
    for parameter in ("a b", "x&y"):
        assert f"tests/acceptance/test_baseline_cases.py::TestGroup::test_pass[{parameter}]" in prompt
    for excluded in ("test_skipped", "test_expected_failure", "test_failed_teardown", "test_future"):
        assert excluded not in prompt
    log = c.store.log_path(1).read_text()
    assert "Acceptance baseline count=2, exit_code=1" in log
    assert "Acceptance baseline verification count=2, exit_code=0" in log
    assert (c.store.root / "runs/issue-1-acceptance-baseline.xml").is_file()


def test_baseline_cache_rebuilds_collection_errors_even_when_pytest_continues(git_history, tmp_path):
    from foragent.run_attempt import AttemptFailure
    from foragent.runner import _capture_acceptance_baseline

    git_history.update({"tests/acceptance/test_broken.py": "def test_broken(:\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    command = f"{shlex.quote(sys.executable)} -m pytest -q --continue-on-collection-errors"
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    ctx = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=FakeTracker(), store=store,
        config=Config(test_command=command), git=GitRepo(git_history.clone), now=lambda: BASE_TIME,
    )

    for _ in range(2):
        with pytest.raises(AttemptFailure, match="acceptance baseline unavailable"):
            _capture_acceptance_baseline(1, ctx, command, git_history.tip)
    assert store.log_path(1).read_text().count("Acceptance baseline count=1, exit_code=1") == 2
    assert not list((store.root.parent / "baselines").glob("*.json"))


@pytest.mark.parametrize("unusable", [
    "timeout", "partial-timeout", "no-junit", "malformed-junit", "collection-error",
    "incomplete", "session-error", "missing-node-id", "invalid-junit",
])
def test_unusable_baseline_stops_before_publish_without_retry(run_case, monkeypatch, unusable):
    c = run_case
    c.history.update({"tests/acceptance/test_keep.py": "def test_keep():\n    assert True\n"})
    c.ctx.config = replace(c.ctx.config, test_command="pytest", max_runtime_minutes=1)
    captures = []

    def check(command, worktree, log, timeout_s, **kwargs):
        words = shlex.split(command)
        report = Path(next(word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")))
        captures.append(timeout_s)
        c.now[0] += timedelta(seconds=20)
        if unusable == "malformed-junit":
            report.write_text("<testsuite")
        elif unusable == "invalid-junit":
            report.write_text("<unrelated/>")
        elif unusable not in {"timeout", "no-junit"}:
            suite = ET.Element("testsuite")
            case = ET.SubElement(suite, "testcase", name="test_keep", classname="tests.acceptance.test_keep")
            if unusable != "missing-node-id":
                case.set("file", "tests/acceptance/test_keep.py")
            if unusable == "collection-error":
                ET.SubElement(case, "error", message="collection failure")
            ET.ElementTree(suite).write(report, encoding="unicode")
        if unusable == "incomplete":
            with log.open("a") as stream:
                stream.write("collected 2 items\n")
        timed_out = unusable in {"timeout", "partial-timeout"}
        code = -9 if timed_out else 1 if unusable in {"collection-error", "session-error"} else 0
        return CheckResult(command, code, timed_out, "")

    monkeypatch.setattr("foragent.runner._check", check)
    assert run_issue(1, c.ctx) == "acceptance-baseline-unavailable"
    assert captures == [60]
    assert c.now[0] == BASE_TIME + timedelta(hours=1, seconds=20)
    assert c.executor.calls == []
    assert c.tracker.created_prs == c.tracker.merged_prs == []
    assert c.store.load_flag(1).reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert "acceptance baseline unavailable" in c.store.get_run(1).detail
    assert parse_reason(c.tracker.list_comments(1)[-1].body) == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert c.store.read_history()[-1].outcome == "acceptance-baseline-unavailable"
    assert not list((c.store.root.parent / "baselines").glob("*.json"))


def test_baseline_uses_a_prebuilt_cache_before_implementation(run_case, monkeypatch):
    from foragent import runner

    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command=f"{shlex.quote(sys.executable)} -m pytest -q")
    keep = "tests/acceptance/test_keep.py"
    c.history.update({keep: "def test_keep():\n    assert True\n"})
    git_command(c.history.clone, "fetch", "-q", "origin")
    original_check = runner._check
    builds = []

    def check(command, worktree, log, timeout_s, **kwargs):
        if any(word.startswith("--junitxml=") for word in shlex.split(command)):
            builds.append(command)
        return original_check(command, worktree, log, timeout_s, **kwargs)

    monkeypatch.setattr(runner, "_check", check)
    baseline = runner._capture_acceptance_baseline(1, c.ctx, c.ctx.config.test_command, c.history.tip)
    assert baseline.node_ids == (f"{keep}::test_keep",)
    assert run_issue(1, c.ctx) == "merged"
    assert len(builds) == 1
    assert f"{keep}::test_keep" in c.executor.calls[0]["prompt"]
    assert "Acceptance baseline count=1, cache=hit" in c.store.log_path(1).read_text()
    assert c.store.get_run(1).attempts == 1


def test_baseline_budget_exhausted_before_capture_stops_with_unavailable_reason(run_case, monkeypatch):
    c = run_case
    c.history.update({"tests/acceptance/test_keep.py": "def test_keep():\n    assert True\n"})
    c.ctx.config = replace(c.ctx.config, test_command="pytest", max_runtime_minutes=1)
    c.now[0] += timedelta(minutes=1)
    monkeypatch.setattr("foragent.runner._check", lambda *args, **kwargs: pytest.fail("no capture budget remains"))

    assert run_issue(1, c.ctx) == "acceptance-baseline-unavailable"
    assert c.executor.calls == []
    assert c.tracker.created_prs == c.tracker.merged_prs == []
    assert c.store.load_flag(1).reason == Reason.ACCEPTANCE_BASELINE_UNAVAILABLE
    assert "run budget exhausted before capture" in c.store.get_run(1).detail
    assert c.store.read_history()[-1].outcome == "acceptance-baseline-unavailable"


def test_baseline_missing_acceptance_directory_still_allows_run(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command="pytest", acceptance_dir="absent-on-base")
    assert run_issue(1, c.ctx) == "merged"
    assert "acceptance directory does not exist" in c.store.log_path(1).read_text()
    assert not (c.store.root / "runs/issue-1-acceptance-baseline.xml").exists()


def test_baseline_ignores_only_existing_files_owned_by_approved_unmerged_issues(git_history, tmp_path):
    from foragent.runner import _capture_acceptance_baseline

    other = "tests/acceptance/test_other issue.py"
    merged = "tests/acceptance/test_merged.py"
    unowned = "tests/acceptance/test_unowned.py"
    missing = "tests/acceptance/test_missing.py"
    git_history.update({path: "def test_keep():\n    assert True\n" for path in (other, merged, unowned)})
    git_command(git_history.clone, "fetch", "-q", "origin")
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    for number, paths in [(1, [ACCEPTANCE]), (2, [other]), (3, [other]), (10, [merged]), (11, [missing])]:
        store.save_approval(
            number, body=BODY, body_sha=body_hash(BODY),
            acceptance_blobs=GitRepo(git_history.clone).blob_shas(git_history.tip, paths),
            approved_by="human", approved_at=BASE_TIME.isoformat(),
        )
    store.start_run(10, branch="merged", worktree=store.worktree_path(10))
    store.finish_run(10, "merged")
    later = StateStore(store.root, now=lambda: BASE_TIME + timedelta(minutes=1))
    later.start_run(10, branch="retried", worktree=store.worktree_path(10))
    later.finish_run(10, "error")
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    command = f"{shlex.quote(sys.executable)} -m pytest -q"
    ctx = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=FakeTracker(), store=store,
        config=Config(test_command=command), git=GitRepo(git_history.clone), now=lambda: BASE_TIME,
    )
    baseline = _capture_acceptance_baseline(1, ctx, command, git_history.tip)
    assert set(baseline.node_ids) == {f"{merged}::test_keep", f"{unowned}::test_keep"}
    log = store.log_path(1).read_text()
    words = shlex.split(log.split("Done when command:\n", 1)[1].splitlines()[0])
    assert {word for word in words if word.startswith("--ignore=")} == {f"--ignore={ACCEPTANCE}", f"--ignore={other}"}
    assert "Acceptance baseline skipped 2 files owned by open issues: #1 #2 #3" in log
    assert "--ignore=" not in baseline.command


def test_baseline_cache_key_tracks_sorted_excluded_paths_and_caches_zero_tests(git_history, tmp_path):
    from foragent.runner import _capture_acceptance_baseline

    other = "tests/acceptance/test_other.py"
    git_history.update({other: "def test_other():\n    assert True\n"})
    git_command(git_history.clone, "fetch", "-q", "origin")
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    command = f"{shlex.quote(sys.executable)} -m pytest -q"
    ctx = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=FakeTracker(), store=store,
        config=Config(test_command=command), git=GitRepo(git_history.clone), now=lambda: BASE_TIME,
    )
    first = _capture_acceptance_baseline(1, ctx, command, git_history.tip)
    assert len(first.node_ids) == 2
    for paths in ([other, ACCEPTANCE], [ACCEPTANCE, other]):
        store.save_approval(
            1, body=BODY, body_sha=body_hash(BODY), acceptance_blobs=dict.fromkeys(paths, "locked"),
            approved_by="human", approved_at=BASE_TIME.isoformat(),
        )
        assert _capture_acceptance_baseline(1, ctx, command, git_history.tip).node_ids == ()
    assert len(list((store.root.parent / "baselines").glob("*.json"))) == 2
    log = store.log_path(1).read_text()
    assert "Acceptance baseline count=0: no acceptance test files remain after exclusions" in log
    assert log.count("cache=hit") == 1


def test_baseline_failure_exhausts_the_done_when_budget(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, max_retries=0, test_command=f"{shlex.quote(sys.executable)} -m pytest -q")
    c.history.update({"tests/acceptance/test_keep.py": '''from pathlib import Path
def test_keep():
    assert not Path("feature.txt").exists(), "existing behavior regressed"
'''})
    assert run_issue(1, c.ctx) == "retries-exceeded"
    assert c.store.get_run(1).attempts == 1
    assert len(c.executor.calls) == 1
    assert c.tracker.merged_prs == []
    assert c.store.load_flag(1).reason == Reason.RETRIES_EXCEEDED
    assert "existing behavior regressed" in c.tracker.list_comments(1)[-1].body
    assert "Acceptance baseline verification count=1, exit_code=1" in c.store.log_path(1).read_text()


@pytest.mark.parametrize("command,reason", [(None, "no test command is known"), ("npm test", "pytest")])
def test_baseline_skip_is_logged_without_creating_a_probe(run_case, monkeypatch, command, reason):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command=command)
    execute = c.executor.run

    def implement(**kwargs):
        # The baseline probe precedes implementation; a post-merge reinstall
        # may independently create a detached worktree afterwards.
        assert not any("--detach" in args for _, args in c.operations)
        return execute(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    assert run_issue(1, c.ctx) == "merged"
    log = c.store.log_path(1).read_text()
    assert "Acceptance baseline skipped (count=0)" in log and reason in log


def test_baseline_uses_detection_and_ignores_full_suite_selection_and_generated_files(run_case, monkeypatch):
    c = run_case
    c.history.update({
        "pytest.ini": "[pytest]\naddopts = --ignore=tests/acceptance\n",
        "tests/acceptance/test_isolated.py": '''from pathlib import Path
def test_isolated():
    if not Path("feature.txt").exists():
        Path("probe-side-effect.txt").write_text("baseline only")
''',
    })
    command = f"{shlex.quote(sys.executable)} -m pytest -q -c pytest.ini --tb short --ignore=tests/acceptance -k nothing tests"
    roots = []

    def detect(root):
        roots.append(root)
        return [command, "npm test"]

    monkeypatch.setattr("foragent.runner.detect_test_commands", detect)
    original = c.executor.run

    def implement(**kwargs):
        assert not (kwargs["worktree"] / "probe-side-effect.txt").exists()
        return original(**kwargs)

    monkeypatch.setattr(c.executor, "run", implement)
    assert run_issue(1, c.ctx) == "merged"
    assert roots and all(root == c.store.worktree_path(1) for root in roots)
    assert "test_isolated" in c.executor.calls[0]["prompt"]
    assert "Acceptance baseline count=1, exit_code=0" in c.store.log_path(1).read_text()
    assert "Acceptance baseline verification count=1, exit_code=0" in c.store.log_path(1).read_text()
    assert not git_command(c.history.origin, "ls-tree", "--name-only", "foragent/issue-1", "probe-side-effect.txt")


def test_baseline_is_captured_once_and_survives_review_retry(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, test_command=f"{shlex.quote(sys.executable)} -m pytest -q")
    keep = "tests/acceptance/test_keep.py"
    c.history.update({keep: "def test_keep():\n    assert True\n"})
    c.executor.steps.append({})
    enable_reviewer(c, ["VERDICT: FAIL\nfeature.txt:1 — fix it", "VERDICT: PASS"])
    assert run_issue(1, c.ctx) == "merged"
    log = c.store.log_path(1).read_text()
    assert log.count("Acceptance baseline count=") == 1
    assert log.count("Acceptance baseline verification count=1, exit_code=0") >= 2
    for call in c.executor.calls:
        assert f"- {keep}::test_keep" in call["prompt"]


def test_empty_baseline_does_not_rerun_failing_tests_or_default_to_the_full_suite(run_case):
    c = run_case
    c.ctx.config = replace(c.ctx.config, acceptance_dir="checks with spaces",
                           test_command=f"{shlex.quote(sys.executable)} -m pytest -q")
    c.history.update({"checks with spaces/test_future.py": "def test_future():\n    assert False\n"})
    assert run_issue(1, c.ctx) == "merged"
    assert len(c.executor.calls) == 1
    assert "test_future" not in c.executor.calls[0]["prompt"]
    log = c.store.log_path(1).read_text()
    assert "Acceptance baseline count=0, exit_code=1" in log
    assert "Acceptance baseline verification" not in log


def test_baseline_lock_wait_is_credited_and_keeps_the_heartbeat(git_history, tmp_path):
    from foragent.runner import _baseline_environment, _capture_acceptance_baseline

    command = f"{shlex.quote(sys.executable)} -m pytest -q"
    store = StateStore(tmp_path / "state", now=lambda: BASE_TIME)
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    mono = [0.0]
    ctx = RunContext(
        repo="owner/repo", repo_root=git_history.clone, tracker=FakeTracker(), store=store,
        config=Config(test_command=command), git=GitRepo(git_history.clone), now=lambda: BASE_TIME,
        monotonic=lambda: mono[0],
    )
    key = sha256(json.dumps(
        [git_history.tip, command, ctx.config.acceptance_dir, _baseline_environment(git_history.clone), []],
        sort_keys=True,
    ).encode()).hexdigest()
    lock_path = store.root.parent / "baselines" / f"{key}.lock"
    lock_path.parent.mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=1) as pool, lock_path.open("a") as lock:
        # Another run holds the baseline lock (for example while it queues for a test slot).
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        future = pool.submit(_capture_acceptance_baseline, 1, ctx, command, git_history.tip)
        deadline = time.monotonic() + 10
        while store.get_run(1).waiting_until is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.get_run(1).waiting_until is not None, "a run waiting on the baseline lock looks stalled"
        mono[0] = 600.0
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        future.result(timeout=60)
    run = store.get_run(1)
    assert run.waited_seconds == 600.0
    assert run.waiting_until is None


def test_log_dropped_ids_names_one_reason_per_id_in_priority_order(tmp_path):
    from foragent.run_baseline import AcceptanceBaseline, log_dropped_ids

    owned, gone, failing, unknown, kept = (f"tests/acceptance/test_{name}.py::test_{name}"
                                           for name in ("owned", "gone", "failing", "unknown", "kept"))
    previous = AcceptanceBaseline("pytest", (owned, gone, failing, unknown, kept))
    new = AcceptanceBaseline("pytest", (kept,), collected_ids=(failing, kept), failing_ids=(failing,),
                             excluded=("tests/acceptance/test_owned.py",))
    log = tmp_path / "run.log"
    # The owned id is also "missing": the first reason in the fixed order wins.
    log_dropped_ids(log, previous, new.node_ids, {owned}, {owned, gone}, new, ci=False)
    assert log.read_text().splitlines() == [
        f"Acceptance baseline dropped protected node id {owned}: owned by an open issue",
        f"Acceptance baseline dropped protected node id {gone}: missing on the new base",
        f"Acceptance baseline dropped protected node id {failing}: failing on the new base",
        f"Acceptance baseline dropped protected node id {unknown}: no longer protected on the new base",
    ]


def test_log_dropped_ids_in_ci_mode_does_not_claim_a_failure(tmp_path):
    from foragent.run_baseline import AcceptanceBaseline, log_dropped_ids

    node = "tests/acceptance/test_a.py::test_a"
    new = AcceptanceBaseline("pytest", (), collected_ids=(node,), failing_ids=(node,))
    log = tmp_path / "run.log"
    log_dropped_ids(log, AcceptanceBaseline("pytest", (node,)), (), set(), set(), new, ci=True)
    assert log.read_text() == f"Acceptance baseline dropped protected node id {node}: no longer protected on the new base\n"
