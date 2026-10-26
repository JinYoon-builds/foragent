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


class _MovedGit:
    """History where the merged target is (or is not) newer than the measured base."""

    def __init__(self, moved=True):
        self.moved, self.calls = moved, []

    def merge_in_progress(self):
        return False

    def _call(self, args):
        self.calls.append(args)
        return "common\n"

    def is_ancestor(self, ancestor, head):
        return not self.moved


def _collector(log, exit_code, ids, timed_out=False):
    """Stand in for pytest --collect-only over the collected item ids `ids`.

    Like pytest, a selector also matches what it expands into (`sel[...]`,
    `sel::...`); exit 4 when a node id selector matches nothing. `exit_code`
    is returned for every other selection (0 for a working project).
    """
    import shlex

    from foragent.run_checks import CheckResult

    calls = []

    def check(command, root):
        calls.append(command)
        words = shlex.split(command)
        selectors = words[words.index("--collect-only") + 1:]
        def found(selector):
            return any(item == selector or item.startswith((selector + "[", selector + "::")) for item in ids)

        code = 4 if exit_code == 0 and any("::" in word and not found(word) for word in selectors) else exit_code
        junit = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
        if junit is not None and exit_code == 0:
            Path(junit).write_text("<testsuites><testsuite/></testsuites>")  # A clean collection.
        return CheckResult(command, code, timed_out, "")
    return check, calls


def test_surviving_baseline_drops_and_logs_only_missing_ids(tmp_path):
    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline

    (tmp_path / "tests/acceptance").mkdir(parents=True)
    (tmp_path / "tests/acceptance/test_a.py").write_text("")
    (tmp_path / "tests/acceptance/test_c.py").write_text("")
    log = tmp_path / "run.log"
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_a",
                                             "tests/acceptance/test_c.py::test_renamed",
                                             "tests/acceptance/test_b.py::test_b"), base_sha="base")
    check, calls = _collector(log, 0, ["tests/acceptance/test_a.py::test_a"])
    kept = surviving_baseline(baseline, _MovedGit(), tmp_path, check, log, "origin/main")
    assert kept.node_ids == ("tests/acceptance/test_a.py::test_a",)
    # The verification's own selectors are tried, even for a file missing on disk.
    words = shlex.split(calls[0])
    assert words[words.index("--collect-only") + 1:] == list(baseline.node_ids)
    # Each absence is confirmed by collecting that one id with a JUnit report.
    probes = [shlex.split(call) for call in calls if "--junitxml=" in call]
    # The directory is collected once as a control that the command has no usage error.
    assert {words[words.index("--collect-only") + 5] for words in probes} == {
        "tests/acceptance/test_c.py::test_renamed", "tests/acceptance/test_b.py::test_b", "tests/acceptance"}
    assert "dropped missing node id: tests/acceptance/test_c.py::test_renamed" in log.read_text()
    assert "dropped missing node id: tests/acceptance/test_b.py::test_b" in log.read_text()


def test_surviving_baseline_keeps_every_id_when_collection_fails(tmp_path):
    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline

    (tmp_path / "tests/acceptance").mkdir(parents=True)
    (tmp_path / "tests/acceptance/test_a.py").write_text("")
    log = tmp_path / "run.log"
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_a",), base_sha="base")
    check, _ = _collector(log, 2, [])
    kept = surviving_baseline(baseline, _MovedGit(), tmp_path, check, log, "origin/main")
    assert kept.node_ids == baseline.node_ids


def test_surviving_baseline_without_acceptance_dir_drops_all_by_collection_only(tmp_path):
    # A missing directory is not evidence on its own; pytest collection reports it.
    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline

    log = tmp_path / "run.log"
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_a",), base_sha="base")
    check, calls = _collector(log, 0, [])
    assert surviving_baseline(baseline, _MovedGit(), tmp_path, check, log, "origin/main").node_ids == ()
    assert calls and all("--collect-only" in shlex.split(call) for call in calls)


def test_surviving_baseline_skips_collection_when_the_base_has_not_moved(tmp_path):
    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline

    log = tmp_path / "run.log"
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_a",), base_sha="base")
    check, calls = _collector(log, 0, [])
    git = _MovedGit(moved=False)
    assert surviving_baseline(baseline, git, tmp_path, check, log, "origin/main") is baseline
    assert calls == [] and git.calls == [["merge-base", "HEAD", "origin/main"]]


def test_base_moved_counts_a_merged_root_conftest_change(tmp_path):
    """A newer main can rename parametrized ids from outside the acceptance directory."""
    import os
    import subprocess

    from foragent.git import GitRepo
    from foragent.run_baseline import _base_moved

    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                   "GIT_INDEX_FILE": str(tmp_path / "scratch-index")}

    def git(*args, input=None):
        return subprocess.run(["git", "-C", str(tmp_path), *args], input=input, env=environment,
                              check=True, capture_output=True, text=True).stdout.strip()

    def snapshot(files, *parents):
        # Plumbing only: the test fixtures forbid porcelain commits.
        (tmp_path / "scratch-index").unlink(missing_ok=True)
        for path, content in files.items():
            blob = git("hash-object", "-w", "--stdin", input=content)
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = git("write-tree")
        return git("commit-tree", tree, *[arg for parent in parents for arg in ("-p", parent)], "-m", "c")

    git("init", "-q", "-b", "main")
    conftest = "import pytest\n@pytest.fixture(params=['old'])\ndef name(request):\n    return request.param\n"
    files = {"tests/acceptance/test_a.py": "def test_a(name):\n    pass\n", "conftest.py": conftest}
    base = snapshot(files)
    work = snapshot({**files, "feature.txt": "work\n"}, base)
    git("update-ref", "refs/heads/main", base)
    git("update-ref", "refs/heads/issue", work)
    git("symbolic-ref", "HEAD", "refs/heads/issue")
    repo = GitRepo(tmp_path)
    assert not _base_moved(repo, base, "main")
    newer = snapshot({**files, "conftest.py": conftest.replace("'old'", "'new'")}, base)
    git("update-ref", "refs/heads/main", newer)
    assert not _base_moved(repo, base, "main")  # main moved, but this tree has not merged it
    merged = snapshot({**files, "conftest.py": conftest.replace("'old'", "'new'"), "feature.txt": "work\n"},
                      work, newer)
    git("update-ref", "refs/heads/issue", merged)
    assert _base_moved(repo, base, "main")


def _real_pytest(tmp_path, ini, command, *, files=None, project_name="project", full=False, node_ids=None):
    """Run the real collection step on a tiny project; return (kept ids, log text)."""
    import subprocess

    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline
    from foragent.run_checks import CheckResult, _pytest_command

    project = tmp_path / project_name
    (project / "tests/acceptance").mkdir(parents=True)
    (project / "pytest.ini").write_text(ini)
    (project / "tests/acceptance/test_a.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('x', [1])\ndef test_kept(x):\n    assert False\n")
    for path, content in (files or {}).items():
        (project / path).parent.mkdir(parents=True, exist_ok=True)
        (project / path).write_text(content)
    log = tmp_path / "run.log"

    def check(command, root):
        done = subprocess.run(["/bin/sh", "-c", command], cwd=root, capture_output=True, text=True)
        log.write_text((log.read_text() if log.exists() else "") + done.stdout + done.stderr)
        return CheckResult(command, done.returncode, False, done.stdout[-4000:])

    baseline = AcceptanceBaseline(_pytest_command(command), node_ids or ("tests/acceptance/test_a.py::test_kept[1]",
                                                                         "tests/acceptance/test_a.py::test_gone"),
                                  base_sha="base")
    kept = surviving_baseline(baseline, _MovedGit(), project, check, log, "origin/main")
    # Nothing is added to the project: the tree is exactly what verification runs on.
    assert not (project / ".foragent").exists()
    return (kept if full else kept.node_ids), log.read_text()


def _python_pytest():
    import shlex
    import sys

    return f"{shlex.quote(sys.executable)} -m pytest"


def test_surviving_baseline_collection_ignores_verbosity_test_cases(tmp_path):
    # A collection tree instead of id lines must not read as "nothing collected".
    kept, log = _real_pytest(tmp_path, "[pytest]\nverbosity_test_cases = 0\naddopts = -q\n", _python_pytest())
    assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log
    assert "dropped missing node id: tests/acceptance/test_a.py::test_gone" in log


def test_surviving_baseline_collection_works_without_the_terminal_plugin(tmp_path):
    # `-p no:terminal` rejects terminal options such as --verbosity.
    kept, log = _real_pytest(tmp_path, "[pytest]\n", f"{_python_pytest()} -p no:terminal")
    assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log
    assert "dropped missing node id: tests/acceptance/test_a.py::test_gone" in log


def test_surviving_baseline_keeps_a_file_that_python_files_no_longer_matches(tmp_path):
    # Verification names the file explicitly, so pytest still runs it there.
    kept, log = _real_pytest(tmp_path, "[pytest]\npython_files = check_*.py\n", _python_pytest())
    assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log


def test_surviving_baseline_keeps_the_commands_own_pythonpath(tmp_path):
    # An inline PYTHONPATH must not hide the plugin, nor lose the project's path.
    files = {"src/helper.py": "VALUE = 1\n",
             "tests/acceptance/test_a.py": "from helper import VALUE\n\n"
                                           "import pytest\n\n@pytest.mark.parametrize('x', [VALUE])\n"
                                           "def test_kept(x):\n    assert False\n"}
    kept, log = _real_pytest(tmp_path, "[pytest]\n", f"PYTHONPATH=src {_python_pytest()}", files=files)
    assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log
    assert "dropped missing node id: tests/acceptance/test_a.py::test_gone" in log


def test_surviving_baseline_collects_with_isolated_python(tmp_path):
    # `python -I` ignores PYTHONPATH; the plugin must load without it.
    kept, log = _real_pytest(tmp_path, "[pytest]\n", _python_pytest().replace(" -m pytest", " -I -m pytest"),
                             project_name="isolated project")
    assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log
    assert "dropped missing node id: tests/acceptance/test_a.py::test_gone" in log


def test_surviving_baseline_collects_when_paths_and_pythonpath_contain_spaces(tmp_path):
    # Spaces in the project (and so the plugin) path and the command's own PYTHONPATH.
    files = {"my src/helper.py": "VALUE = 1\n",
             "tests/acceptance/test_a.py": "from helper import VALUE\n\n"
                                           "import pytest\n\n@pytest.mark.parametrize('x', [VALUE])\n"
                                           "def test_kept(x):\n    assert False\n"}
    kept, log = _real_pytest(tmp_path, "[pytest]\n", f"PYTHONPATH='my src' {_python_pytest()}", files=files,
                             project_name="my project")
    assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log
    assert "dropped missing node id: tests/acceptance/test_a.py::test_gone" in log


def test_surviving_baseline_keeps_project_conftest_imports_and_noconftest(tmp_path):
    # Tests that import the project's conftest, with and without --noconftest.
    files = {"tests/acceptance/conftest.py": "VALUE = 1\n",
             "tests/acceptance/test_a.py": "from conftest import VALUE\n\n"
                                           "import pytest\n\n@pytest.mark.parametrize('x', [VALUE])\n"
                                           "def test_kept(x):\n    assert False\n"}
    for suffix, name in (("", "imports conftest"), (" --noconftest", "no conftest")):
        kept, log = _real_pytest(tmp_path / name, "[pytest]\n", f"{_python_pytest()}{suffix}", files=files)
        assert kept == ("tests/acceptance/test_a.py::test_kept[1]",), log
        assert "dropped missing node id: tests/acceptance/test_a.py::test_gone" in log


def test_surviving_baseline_keeps_ids_of_a_file_that_no_longer_imports(tmp_path):
    # pytest reports "not found" for every id of a file that fails to import;
    # that is a failure to verify, not a renamed test.
    files = {"tests/acceptance/test_a.py": "import missing_module\n\ndef test_kept():\n    pass\n"}
    kept, log = _real_pytest(tmp_path, "[pytest]\n", _python_pytest(), files=files, full=True)
    assert kept.node_ids == () and kept.unverifiable == (
        "tests/acceptance/test_a.py::test_kept[1]", "tests/acceptance/test_a.py::test_gone"), log
    assert "dropped missing node id" not in log


def test_surviving_baseline_keeps_an_expanded_selector_for_the_exact_check(tmp_path):
    # `::test_keep` stays a valid selector once main parametrizes it; collection
    # alone cannot tell, so verification must confirm the exact id.
    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline

    (tmp_path / "tests/acceptance").mkdir(parents=True)
    (tmp_path / "tests/acceptance/test_a.py").write_text("")
    log = tmp_path / "run.log"
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_keep",), base_sha="base")
    check, _ = _collector(log, 0, ["tests/acceptance/test_a.py::test_keep[1]", "tests/acceptance/test_a.py::test_keep[2]"])
    kept = surviving_baseline(baseline, _MovedGit(), tmp_path, check, log, "origin/main")
    assert kept.node_ids == baseline.node_ids and kept.exact_check


def _verify_exact(tmp_path, source, node_ids, files=None):
    """Run the real verification command; return (result, log text, command)."""
    import subprocess

    from foragent.run_baseline import (AcceptanceBaseline, baseline_verification_command,
                                       exact_baseline_result)
    from foragent.run_checks import CheckResult, _pytest_command

    project = tmp_path / "my project"
    (project / "tests/acceptance").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "tests/acceptance/test_a.py").write_text(source)
    for path, content in (files or {}).items():
        (project / path).write_text(content)
    log = tmp_path / "run.log"
    log.write_text("")
    baseline = AcceptanceBaseline(_pytest_command(_python_pytest()), node_ids, base_sha="base", exact_check=True)
    report = tmp_path / "state dir/runs/verification.xml"
    command = baseline_verification_command(baseline, report)
    def check(command, root):
        done = subprocess.run(["/bin/sh", "-c", command], cwd=root, capture_output=True, text=True)
        return CheckResult(command, done.returncode, False, done.stdout + done.stderr)

    run = check(command, project)
    result = exact_baseline_result(baseline, report, run, log, check, project)
    return result, log.read_text(), command


PARAMETRIZED = ("import pytest\n\n@pytest.mark.parametrize('x', [1, 2])\n"
                "def test_keep(x):\n    assert False, 'new variant'\n\ndef test_other():\n    pass\n")


def test_exact_check_drops_a_parametrized_id_and_still_verifies_the_rest(tmp_path):
    result, log, _ = _verify_exact(tmp_path, PARAMETRIZED, ("tests/acceptance/test_a.py::test_keep",
                                                           "tests/acceptance/test_a.py::test_other"))
    # The new variants did not run in place of the old id; test_other still did.
    assert result.exit_code == 0, result.output
    assert "dropped missing node id: tests/acceptance/test_a.py::test_keep (not collected after the sync)" in log
    assert "test_other" not in log


def test_exact_check_skips_when_only_expanded_variants_remain(tmp_path):
    result, log, command = _verify_exact(tmp_path, PARAMETRIZED, ("tests/acceptance/test_a.py::test_keep",))
    assert result.exit_code == 0, result.output
    assert "verification skipped (count=0)" in log
    # Only the non-parametrized id's own expansions are deselected; each option and
    # the report path with spaces stay one word each.
    words = shlex.split(command)
    assert [word for word in words if word.startswith("--deselect")] == [
        "--deselect=tests/acceptance/test_a.py::test_keep[", "--deselect=tests/acceptance/test_a.py::test_keep::"]
    assert any(word.endswith("state dir/runs/verification.xml") for word in words)


def test_exact_check_still_fails_an_exact_baseline_test(tmp_path):
    source = "def test_keep():\n    assert False, 'regressed'\n"
    result, log, _ = _verify_exact(tmp_path, source, ("tests/acceptance/test_a.py::test_keep",))
    assert result.exit_code == 1
    assert "dropped" not in log


def test_exact_check_never_matches_ids_by_prefix(tmp_path):
    # `test_keep[a]` is a prefix of `test_keep[a][b]`; both are exact baseline ids.
    source = ("import pytest\n\n@pytest.mark.parametrize('v', [pytest.param(1, id='a'), "
              "pytest.param(2, id='a][b')])\ndef test_keep(v):\n    assert v == 1, 'regressed'\n")
    ids = ("tests/acceptance/test_a.py::test_keep[a]", "tests/acceptance/test_a.py::test_keep[a][b]")
    result, log, command = _verify_exact(tmp_path, source, ids)
    assert result.exit_code == 1 and "test_keep[a][b]" in result.output
    assert "dropped" not in log
    # Parametrized ids select exactly; nothing is deselected for them.
    assert "--deselect" not in command


IMPORT_SKIP = "import pytest\n\npytest.importorskip('no_such_module')\n\ndef test_kept():\n    pass\n"


def test_module_skipped_at_import_is_unverifiable_not_deleted(tmp_path):
    # The selector exits 4 and the file exits 5: no proof of deletion.
    from foragent.run_baseline import exact_baseline_result

    files = {"tests/acceptance/test_a.py": IMPORT_SKIP}
    kept, log = _real_pytest(tmp_path, "[pytest]\n", _python_pytest(), files=files, full=True)
    assert kept.node_ids == () and len(kept.unverifiable) == 2, log
    assert "dropped" not in log
    result = exact_baseline_result(kept, tmp_path / "unused.xml", None, tmp_path / "run.log", None, tmp_path)
    assert result.exit_code == 1 and "could not be verified" in result.output
    assert "verification skipped" not in log


def test_exact_check_keeps_an_id_whose_module_skips_while_others_run(tmp_path):
    # A run that reports only the module's skip proves nothing about its tests.
    from foragent.run_baseline import AcceptanceBaseline, exact_baseline_result
    from foragent.run_checks import CheckResult

    report = tmp_path / "report.xml"
    report.write_text('<testsuites><testsuite><testcase classname="" name="tests.acceptance.test_a" '
                      'file="tests/acceptance/test_a.py"><skipped/></testcase>'
                      '<testcase classname="tests.acceptance.test_b" name="test_b" '
                      'file="tests/acceptance/test_b.py"/></testsuite></testsuites>')
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_kept",
                                             "tests/acceptance/test_b.py::test_b"), exact_check=True)
    log = tmp_path / "run.log"
    log.write_text("")

    def check(command, root):  # The probe of test_kept: pytest reports a collection skip.
        junit = next(word.split("=", 1)[1] for word in shlex.split(command) if word.startswith("--junitxml="))
        Path(junit).write_text(report.read_text())
        return CheckResult(command, 5, False, "")

    result = exact_baseline_result(baseline, report, CheckResult("pytest", 0, False, ""), log, check, tmp_path)
    assert result.exit_code == 1
    assert "could not be verified (not run, and no proof they were deleted): tests/acceptance/test_a.py::test_kept" \
        in result.output
    assert "dropped" not in log.read_text()


def test_missing_ids_needs_a_clean_file_collection_to_prove_deletion(tmp_path):
    # The one rule: collecting the id ran and did not collect it (exit 4/5, no record).
    from foragent.run_baseline import AcceptanceBaseline, surviving_baseline
    from foragent.run_checks import CheckResult

    (tmp_path / "tests/acceptance").mkdir(parents=True)
    (tmp_path / "tests/acceptance/test_a.py").write_text("")
    log = tmp_path / "run.log"
    baseline = AcceptanceBaseline("pytest", ("tests/acceptance/test_a.py::test_a",), base_sha="base")
    empty = "<testsuites><testsuite/></testsuites>"
    skipped = ('<testsuites><testsuite><testcase classname="" name="tests.acceptance.test_a" '
               'file="tests/acceptance/test_a.py"><skipped message="collection skipped"/></testcase>'
               '</testsuite></testsuites>')
    errored = skipped.replace('<skipped message="collection skipped"/>', '<error message="collection failure"/>')
    # (probe exit, probe timed out, probe JUnit report, exit without our deselects, directory exit, deleted)
    cases = ((4, False, empty, 0, 0, True), (5, False, empty, 0, 0, True),
             (4, False, empty, 0, 4, False),   # A hook raising UsageError: the directory exits 4 too.
             (5, False, empty, 5, 0, False),   # A hook deselecting the test: 5 without our deselects too.
             (4, False, skipped, 0, 0, False), (4, False, errored, 0, 0, False), (4, False, None, 0, 0, False),
             (4, True, empty, 0, 0, False), (0, False, empty, 0, 0, False), (2, False, empty, 0, 0, False),
             (3, False, empty, 0, 0, False))
    for probe_exit, timed_out, report, bare_exit, directory_exit, deleted in cases:
        def check(command, root, probe_exit=probe_exit, timed_out=timed_out, report=report,
                  bare_exit=bare_exit, directory_exit=directory_exit):
            words = shlex.split(command)
            junit = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
            if junit is None:
                return CheckResult(command, 4, False, "")  # The group selector is not found.
            selectors = [word for word in words[words.index("--collect-only") + 1:] if not word.startswith("-")]
            if not any("::" in word for word in selectors):
                Path(junit).write_text(empty)
                return CheckResult(command, directory_exit, False, "")  # The directory control.
            if not any(word.startswith("--deselect") for word in words):
                Path(junit).write_text(empty)
                return CheckResult(command, bare_exit, False, "")  # The id without our deselects.
            if report is not None:
                Path(junit).write_text(report)
            return CheckResult(command, probe_exit, timed_out, "")
        kept = surviving_baseline(baseline, _MovedGit(), tmp_path, check, log, "origin/main")
        expected = () if deleted else baseline.node_ids
        assert kept.node_ids == () and kept.unverifiable == expected, (probe_exit, timed_out, report)


def test_deleting_the_last_test_of_a_kept_file_drops_its_id(tmp_path):
    # The file stays and collects nothing: a clean, empty collection is deletion.
    files = {"tests/acceptance/test_a.py": "import pytest\n"}
    kept, log = _real_pytest(tmp_path, "[pytest]\n", _python_pytest(), files=files, full=True)
    assert kept.node_ids == () and kept.unverifiable == (), log
    assert "dropped missing node id: tests/acceptance/test_a.py::test_kept[1]" in log


def test_exact_check_keeps_a_session_failure(tmp_path):
    # pytest exits 1 from the session although every baseline record passed.
    conftest = "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n"
    result, _, _ = _verify_exact(tmp_path, "def test_keep():\n    pass\n",
                                 ("tests/acceptance/test_a.py::test_keep",),
                                 files={"tests/acceptance/conftest.py": conftest})
    assert result.exit_code == 1


def test_exact_check_reads_a_parameter_id_containing_the_separator(tmp_path):
    # `a::b` is one parameter id, not a nested node; test_keep was parametrized.
    source = ("import pytest\n\n@pytest.mark.parametrize('v', [1], ids=['a::b'])\n"
              "def test_keep(v):\n    assert False, 'new variant'\n\ndef test_other():\n    pass\n")
    result, log, _ = _verify_exact(tmp_path, source, ("tests/acceptance/test_a.py::test_keep",
                                                     "tests/acceptance/test_a.py::test_other"))
    assert result.exit_code == 0, result.output
    assert "dropped missing node id: tests/acceptance/test_a.py::test_keep (not collected after the sync)" in log


def test_node_parts_keep_parameters_whole():
    from foragent.run_baseline import _node_parts

    assert _node_parts("t/test_a.py::TestC::test_m[a::b][c]") == ("t/test_a.py", ("TestC", "test_m"), "[a::b][c]")
    assert _node_parts("t/test_a.py::test_x") == ("t/test_a.py", ("test_x",), None)


def test_partial_collection_of_a_file_is_not_deletion(tmp_path):
    # pytest_generate_tests skips TestC: the file still exits 0, but with a skip record.
    source = ("import pytest\n\nclass TestC:\n    def test_m(self):\n        pass\n\n"
              "def test_other():\n    pass\n\n"
              "def pytest_generate_tests(metafunc):\n    if metafunc.cls is TestC:\n        pytest.skip('gen')\n")
    files = {"tests/acceptance/test_a.py": source}
    ids = ("tests/acceptance/test_a.py::TestC::test_m", "tests/acceptance/test_a.py::test_other")
    kept, log = _real_pytest(tmp_path, "[pytest]\n", _python_pytest(), files=files, full=True, node_ids=ids)
    assert kept.unverifiable == ("tests/acceptance/test_a.py::TestC::test_m",), log
    assert kept.node_ids == ("tests/acceptance/test_a.py::test_other",)
    assert "dropped" not in log


def test_expanded_variants_never_run_after_their_id_is_gone(tmp_path):
    # A new variant that calls pytest.exit would exit 2; it must not run at all.
    source = ("import pytest\n\n@pytest.mark.parametrize('v', [1])\n"
              "def test_keep(v):\n    pytest.exit('variant ran')\n")
    result, log, _ = _verify_exact(tmp_path, source, ("tests/acceptance/test_a.py::test_keep",))
    assert result.exit_code == 0, result.output
    assert "variant ran" not in result.output
    assert "dropped missing node id: tests/acceptance/test_a.py::test_keep (not collected after the sync)" in log
    assert "verification skipped (count=0)" in log


def test_a_failing_replacement_does_not_excuse_a_session_failure(tmp_path):
    # Surviving test passes, the replacement variant fails, and the session also fails.
    source = ("import pytest\n\n@pytest.mark.parametrize('v', [1])\n"
              "def test_keep(v):\n    assert False, 'new variant'\n\ndef test_other():\n    pass\n")
    conftest = "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n"
    result, _, _ = _verify_exact(tmp_path, source, ("tests/acceptance/test_a.py::test_keep",
                                                   "tests/acceptance/test_a.py::test_other"),
                                 files={"tests/acceptance/conftest.py": conftest})
    assert result.exit_code == 1
    assert "new variant" not in result.output


def test_files_created_by_session_hooks_are_not_deleted(tmp_path):
    # The file exists only while pytest runs; disk absence before pytest is no evidence.
    import subprocess

    from foragent.run_baseline import (AcceptanceBaseline, baseline_verification_command,
                                       exact_baseline_result, surviving_baseline)
    from foragent.run_checks import CheckResult, _pytest_command

    project = tmp_path / "my project"
    (project / "tests/acceptance").mkdir(parents=True)
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "conftest.py").write_text(
        "from pathlib import Path\n\nGENERATED = Path('tests/acceptance/test_gen.py')\n\n"
        "def pytest_sessionstart(session):\n    GENERATED.write_text(\"def test_g():\\n    assert False, 'broken'\\n\")\n\n"
        "def pytest_sessionfinish(session):\n    GENERATED.unlink()\n")
    log = tmp_path / "run.log"
    log.write_text("")

    def check(command, root):
        done = subprocess.run(["/bin/sh", "-c", command], cwd=root, capture_output=True, text=True)
        return CheckResult(command, done.returncode, False, done.stdout + done.stderr)

    baseline = AcceptanceBaseline(_pytest_command(_python_pytest()), ("tests/acceptance/test_gen.py::test_g",),
                                  base_sha="base")
    kept = surviving_baseline(baseline, _MovedGit(), project, check, log, "origin/main")
    assert kept.node_ids == baseline.node_ids and kept.unverifiable == (), log.read_text()
    report = tmp_path / "verification.xml"
    result = exact_baseline_result(kept, report, check(baseline_verification_command(kept, report), project),
                                   log, check, project)
    assert result.exit_code == 1 and "broken" in result.output
    assert "dropped" not in log.read_text()


def test_disappearance_is_detected_and_logged_while_another_test_fails(tmp_path):
    # test_keep was parametrized away; test_other still exists and now fails.
    source = ("import pytest\n\n@pytest.mark.parametrize('v', [1])\n"
              "def test_keep(v):\n    pass\n\ndef test_other():\n    assert False, 'regressed'\n")
    result, log, _ = _verify_exact(tmp_path, source, ("tests/acceptance/test_a.py::test_keep",
                                                     "tests/acceptance/test_a.py::test_other"))
    assert result.exit_code == 1 and "regressed" in result.output
    assert "dropped missing node id: tests/acceptance/test_a.py::test_keep" in log


def test_an_id_that_does_not_run_in_a_crashed_run_is_not_confirmed(tmp_path):
    # A non-collection exit (pytest.exit -> 2) keeps an unproven id protected and logged.
    source = ("import pytest\n\ndef test_first():\n    pytest.exit('stop')\n\ndef test_second():\n    pass\n")
    result, log, _ = _verify_exact(tmp_path, source, ("tests/acceptance/test_a.py::test_first",
                                                     "tests/acceptance/test_a.py::test_second"))
    assert result.exit_code == 2
    assert "did not run and is not proven deleted: tests/acceptance/test_a.py::test_second" in log
    assert "dropped" not in log


USAGE_HOOK = "import pytest\n\ndef pytest_collection_finish(session):\n    raise pytest.UsageError('hook')\n"
DESELECT_HOOK = ("def pytest_collection_modifyitems(config, items):\n"
                 "    gone = [item for item in items if item.name == 'test_keep']\n"
                 "    items[:] = [item for item in items if item not in gone]\n"
                 "    config.hook.pytest_deselected(items=gone)\n")
FAILING_KEEP = "def test_keep():\n    assert False, 'broken'\n\ndef test_other():\n    pass\n"


def test_a_hook_usage_error_is_not_absence(tmp_path):
    # The hook makes every collection exit 4 with no record; the test still exists.
    files = {"tests/acceptance/test_a.py": FAILING_KEEP, "conftest.py": USAGE_HOOK}
    ids = ("tests/acceptance/test_a.py::test_keep",)
    kept, log = _real_pytest(tmp_path / "phase", "[pytest]\n", _python_pytest(), files=files, full=True, node_ids=ids)
    assert kept.unverifiable == ids and "dropped" not in log
    result, log, _ = _verify_exact(tmp_path / "verify", FAILING_KEEP, ids, files={"conftest.py": USAGE_HOOK})
    assert result.exit_code != 0 and "dropped" not in log


def test_a_hook_deselecting_an_existing_failing_test_is_not_absence(tmp_path):
    files = {"tests/acceptance/test_a.py": FAILING_KEEP, "conftest.py": DESELECT_HOOK}
    ids = ("tests/acceptance/test_a.py::test_keep",)
    kept, log = _real_pytest(tmp_path / "phase", "[pytest]\n", _python_pytest(), files=files, full=True, node_ids=ids)
    # The selector is found, so collection keeps it for verification.
    assert kept.node_ids == ids and "dropped" not in log
    result, log, _ = _verify_exact(tmp_path / "verify", FAILING_KEEP, ids, files={"conftest.py": DESELECT_HOOK})
    assert result.exit_code != 0
    assert "not proven deleted: tests/acceptance/test_a.py::test_keep" in log and "dropped" not in log
