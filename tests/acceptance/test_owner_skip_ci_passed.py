"""Merge owner checks follow D124: skip exactly what the checked commit's CI passed.

Real runner, owner tick, Git and pytest on the small repository of the locked
merge-path contract; only GitHub, the models and the clock are fake. Each small
test writes one marker line (its name and its working directory) when pytest
really executes it, so the tests see what ran on this machine and where. Only
markers written in the owner's merge-owner-* worktrees count: the lane workers
and the offline CI measurement write theirs elsewhere.

Log lines (format is the lane's choice) are read only from merge-owner.log. The
real owner process entry point writes there through its stdout/stderr transport. A
start or end line contains the word "check" and names the batch branch, a member
"#N" or the checked commit (7+ hex digits). The end line gives elapsed seconds as
"<number>s", "<number> seconds" or "elapsed <number>"; the start line has none.
"""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from datetime import timedelta
import os
import re
import time

from conftest import git_command
from test_merge_owner_batch import batched, groups, update_base  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from acceptance.test_merge_path_real_settings import PYTHON, world  # noqa: F401
from foragent import git, merge_owner, merge_owner_batch as batch, runner, state, tracker
from foragent.issue import acceptance_paths, body_hash
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore

OWN = "tests/acceptance/test_own_{n}.py"
CI_COVERED = "tests/test_ci_covered.py::test_ci_covered"
NOT_IN_CI = "tests/test_not_in_ci.py::test_not_in_ci"
MACHINE = "tests/test_machine_only.py::test_machine_only"
UNLOCKED = "tests/acceptance/test_unlocked_extra.py::test_unlocked_extra"
CONFLICT = "tests/test_tip_conflict.py::test_tip_conflict"
OWNER_WORKTREE = "merge-owner-"
ELAPSED = (re.compile(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)\b", re.I),
           re.compile(r"(?:elapsed|took|duration)\D{0,3}(\d+(?:\.\d+)?)", re.I))


def _marking(marks, name, body="    pass\n", *, marker=""):
    return ("import os\nfrom pathlib import Path\nimport pytest\n\n"
            f"{marker}def {name}():\n"
            f"    with open({str(marks)!r}, 'a') as stream:\n"
            f"        stream.write({name!r} + '\\t' + os.getcwd() + '\\n')\n" + body)


def small_repo_files(marks):
    own_5 = _marking(marks, "test_own", "    assert Path('src/member_5.py').is_file()\n")
    return {
        OWN.format(n=5): own_5,
        OWN.format(n=1): _marking(marks, "test_own_1", "    assert Path('src/member_1.py').is_file()\n"),
        "tests/test_ci_covered.py": _marking(marks, "test_ci_covered"),
        "tests/test_not_in_ci.py": _marking(marks, "test_not_in_ci"),
        "tests/test_machine_only.py": _marking(marks, "test_machine_only",
                                               marker="@pytest.mark.local_only\n"),
        "tests/acceptance/test_unlocked_extra.py": _marking(marks, "test_unlocked_extra"),
        # Each member passes it alone; only the composed tip has both member files.
        "tests/test_tip_conflict.py": _marking(
            marks, "test_tip_conflict",
            "    assert not (Path('src/member_5.py').exists() and Path('src/member_1.py').exists())\n"),
    }


def set_done_when(case, n, commands):
    """Give member n these Done when commands; its locked files follow from them as at approval."""
    approval = case.store.load_approval(n)
    body = re.sub(r"## Done when\n```sh\n.*?```\n", "## Done when\n```sh\n" + "".join(
        f"{command}\n" for command in commands) + "```\n", approval.body, flags=re.S)
    locked = acceptance_paths(commands, case.config.acceptance_dir)
    case.tracker.issues[n] = replace(case.tracker.get_issue(n), body=body)
    case.store.save_approval(**replace(approval, body=body, body_sha=body_hash(body),
                                       acceptance_blobs=case.git.blob_shas(case.base, locked)).__dict__)
    return locked


def member_5_commands():
    pytest_ = f"{PYTHON} -m pytest -q -p no:cacheprovider"
    return [
        f"{pytest_} {OWN.format(n=5)} tests/test_ci_covered.py tests/test_machine_only.py",
        # `tests` is not in the acceptance directory, so this command locks nothing new.
        f"{pytest_} tests -k 'unlocked_extra or not_in_ci'",
    ]


def owner_runs(marks):
    """How often each small test ran inside a merge owner worktree."""
    counts = {}
    if marks.exists():
        for line in marks.read_text().splitlines():
            name, cwd = line.split("\t", 1)
            if OWNER_WORKTREE in cwd:
                counts[name] = counts.get(name, 0) + 1
    return counts


def build_batch(case, tmp_path, *, done_when=None):
    marks = tmp_path / "owner-marks.txt"
    update_base(case, small_repo_files(marks))
    for n, commands in (done_when or {
        5: member_5_commands(),
        1: [f"{PYTHON} -m pytest -q -p no:cacheprovider {OWN.format(n=1)}"],
    }).items():
        assert OWN.format(n=n) in set_done_when(case, n, commands)
    support.queue_workers(case, [5, 1])
    member_heads = {case.tracker.pr_head_sha(support.pr_for(case, n).number) for n in (5, 1)}
    marks.unlink(missing_ok=True)  # Only the owner pass counts from here on.
    return marks, member_heads


def tip_of(case):
    prefixes = groups(case)
    return case.tracker.pr_head_sha(prefixes[-1].number) if prefixes else None


def unit_receipts(case, monkeypatch, member_heads, *, tip_set):
    """CI's exact-head unit passes: tip_set on the published tip; another set on member heads."""
    lookups = []
    on_members = {CI_COVERED, NOT_IN_CI, CONFLICT, MACHINE}

    def receipt(head):
        lookups.append(head)
        if head == tip_of(case):
            return None if tip_set is None else (42, set(tip_set))
        if head in member_heads:
            return 7, set(on_members)
        return None

    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", receipt)
    return lookups


def owner_pass(case, monkeypatch):
    """Run the public tick and real process entry point with offline transports.

    Like Popen in merge_owner.start, redirect the process's stdout/stderr to
    merge-owner.log while holding its inherited owner lock. Never copy actions
    into the file ourselves: run_process must actually emit them.
    """
    started = time.monotonic()
    case.clock[0] += timedelta(seconds=1)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.ctx.store = case.store
    tick(TickContext(
        "owner/repo", case.root, case.tracker, case.store, case.config, case.git,
        now=lambda: case.clock[0], launcher=lambda n: case.launches.append(n), start_runs=False,
        baseline_launcher=lambda *args: None,
    ))
    advanced = 0
    context, run_logged, git_init = runner.RunContext, runner.run_logged, git.GitRepo.__init__

    def owner_context(*args, **kwargs):
        ctx = context(*args, **kwargs)
        ctx.now = lambda: case.clock[0]
        ctx.monotonic = lambda: time.monotonic() + advanced
        return ctx

    def measurable_check(*args, **kwargs):
        nonlocal advanced
        result = run_logged(*args, **kwargs)
        if OWNER_WORKTREE in str(kwargs.get("cwd", "")):
            # A check consumes positive time even when the rest of the fake
            # clock stands still. Real execution is still measured as well.
            advanced += 2
            case.clock[0] += timedelta(seconds=2)
        return result

    def offline_git(repo, root, run=None):
        git_init(repo, root, run if run is not None else case.git._run)

    with monkeypatch.context() as patch:
        patch.setattr(runner, "RunContext", owner_context)
        patch.setattr(runner, "run_logged", measurable_check)
        patch.setattr(git.GitRepo, "__init__", offline_git)
        patch.setattr(tracker, "GhTracker", lambda repo: case.tracker)
        patch.setattr(state, "StateStore", lambda root: case.store)
        while case.owner_starts:
            ctx = case.owner_starts.pop(0)
            with ctx.store.tick_lock() as released:
                assert released, "Owner work still runs under tick.lock"
            lock = ctx.store.lock_acceptance_baseline("merge-owner", blocking=False)
            assert lock is not None, "the owner process did not acquire its lock"
            with lock, (ctx.store.root / "merge-owner.log").open("a") as stream:
                with redirect_stdout(stream), redirect_stderr(stream):
                    merge_owner.run_process({
                        "repo": ctx.repo, "repo_root": str(ctx.repo_root),
                        "store_root": str(ctx.store.root), "config": asdict(ctx.config),
                        "committed_config": False,
                    }, os.dup(lock.fileno()))
    return time.monotonic() - started, advanced


def owner_output(case):
    path = case.store.root / "merge-owner.log"
    assert path.is_file(), "the owner process wrote no merge-owner.log"
    return path.read_text().splitlines()


def _elapsed(line):
    for pattern in ELAPSED:
        if match := pattern.search(line):
            return float(match.group(1))
    return None


def check_lines(lines, names):
    """Start and end lines of machine checks that name the batch, an issue or the checked commit."""
    def named(line):
        if any(name in line for name in names["literal"]):
            return True
        return any(len(token) >= 7 and any(sha.startswith(token) for sha in names["shas"])
                   for token in re.findall(r"\b[0-9a-f]{7,40}\b", line))

    relevant = [line for line in lines if "check" in line.lower() and named(line)
                and not any(word in line for word in ("retired:", "reaped", "cleanup"))]
    ends = [line for line in relevant if _elapsed(line) is not None]
    starts = [line for line in relevant if _elapsed(line) is None]
    return starts, ends


def assert_one_timed_pass(case, names, wall_seconds, clock_advance):
    lines = owner_output(case)
    starts, ends = check_lines(lines, names)
    assert len(starts) == 1, f"expected one start line of the owner's machine checks, got {starts!r} in {lines!r}"
    assert len(ends) == 1, f"expected one end line with elapsed seconds, got {ends!r} in {lines!r}"
    assert clock_advance > 0, "the owner ran no measurable machine check"
    assert 0 < _elapsed(ends[0]) <= wall_seconds + clock_advance, ends[0]


def test_batch_tip_skips_tests_its_ci_passed_and_runs_locked_machine_and_other_tests(
        batched, monkeypatch, tmp_path):
    case = batched
    marks, member_heads = build_batch(case, tmp_path)
    unit_receipts(case, monkeypatch, member_heads,
                  tip_set={CI_COVERED, MACHINE, f"{OWN.format(n=5)}::test_own"})
    owner_pass(case, monkeypatch)

    tip = tip_of(case)
    assert len(case.target_pushes) == 1 and case.history.tip == tip, "the batch did not move main"
    assert len(case.store.owner_merge_facts()) == 2
    ran = owner_runs(marks)
    # Unit receipt and acceptance reports of the tip both list these as passed.
    assert ran.get("test_ci_covered", 0) == 0, f"owner re-ran a test the tip's CI passed: {ran}"
    assert ran.get("test_unlocked_extra", 0) == 0, f"owner re-ran an acceptance test the tip's CI passed: {ran}"
    # Not in the tip's passing set (only in a member head's): it runs.
    assert ran.get("test_not_in_ci", 0) == 1, ran
    # Locked (the member's own Done when acceptance file) and machine-only tests always run.
    assert ran.get("test_own", 0) == 1, ran
    assert ran.get("test_own_1", 0) == 1, ran
    assert ran.get("test_machine_only", 0) == 1, ran


def test_unreadable_tip_passing_set_runs_every_check_and_logs_the_pass(
        batched, monkeypatch, tmp_path):
    case = batched
    marks, member_heads = build_batch(case, tmp_path)
    # Member heads have receipts, the checked tip has none: nothing may be skipped from them.
    unit_receipts(case, monkeypatch, member_heads, tip_set=None)
    wall, advanced = owner_pass(case, monkeypatch)

    tip = tip_of(case)
    assert len(case.target_pushes) == 1 and case.history.tip == tip
    ran = owner_runs(marks)
    assert ran == {name: 1 for name in (
        "test_ci_covered", "test_not_in_ci", "test_own", "test_own_1",
        "test_machine_only", "test_unlocked_extra",
    )}, f"missing tip evidence must run every required check exactly once: {ran}"
    names = {"literal": [batch._name(case.base, 2), "#5", "#1"], "shas": [tip]}
    assert_one_timed_pass(case, names, wall, advanced)


def test_merge_owner_log_shows_start_and_end_of_the_tips_machine_checks(batched, monkeypatch):
    case = batched
    support.queue_workers(case, [5, 1])
    base = case.history.tip
    wall, advanced = owner_pass(case, monkeypatch)

    tip = tip_of(case)
    assert len(case.target_pushes) == 1 and case.history.tip == tip
    names = {"literal": [batch._name(base, 2), "#5", "#1"], "shas": [tip]}
    assert_one_timed_pass(case, names, wall, advanced)


def test_unavailable_checked_commit_acceptance_reports_runs_every_required_check(
        proto, monkeypatch, tmp_path):
    """A refreshed head lacks acceptance evidence; every required check still runs."""
    case = proto
    marks = tmp_path / "owner-marks.txt"
    update_base(case, small_repo_files(marks))
    set_done_when(case, 5, member_5_commands())
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    member = case.tracker.pr_head_sha(pr)
    marks.unlink(missing_ok=True)
    case.history.update({"src/neighbor.py": "VALUE = 'requires refreshed checks'\n"})
    moved = case.history.tip
    reports, lookups = case.tracker.pr_acceptance_reports, []

    def unavailable(head):
        if head != member:
            lookups.append(head)
            return []
        return reports(head)

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", unavailable)
    # Unit evidence is readable, but only credits checks that must run locally
    # anyway. The unlocked acceptance test has no readable CI passing list.
    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", lambda head:
                        (42, {MACHINE, f"{OWN.format(n=5)}::test_own"}))
    wall, advanced = owner_pass(case, monkeypatch)
    checked = case.tracker.pr_head_sha(pr)
    assert checked != member, "the owner did not publish its refreshed checked commit"
    # The next process sees the published head's missing acceptance artifacts.
    owner_pass(case, monkeypatch)
    assert checked in lookups, "the unavailable reports did not belong to the checked commit"
    assert not case.tracker.merged_prs and case.history.tip == moved
    ran = owner_runs(marks)
    assert ran == {name: 1 for name in (
        "test_ci_covered", "test_not_in_ci", "test_own", "test_machine_only", "test_unlocked_extra",
    )}, f"missing acceptance evidence must run every required check exactly once: {ran}"
    assert_one_timed_pass(case, {"literal": ["#5"], "shas": [checked]}, wall, advanced)


def test_failing_member_check_still_stops_the_batch(batched, monkeypatch, tmp_path):
    case = batched
    pytest_ = f"{PYTHON} -m pytest -q -p no:cacheprovider"
    marks, member_heads = build_batch(case, tmp_path, done_when={
        5: [f"{pytest_} {OWN.format(n=5)} tests/test_ci_covered.py"],
        1: [f"{pytest_} {OWN.format(n=1)} tests/test_tip_conflict.py"],
    })
    unit_receipts(case, monkeypatch, member_heads, tip_set={CI_COVERED})
    base = case.history.tip
    wall, advanced = owner_pass(case, monkeypatch)

    assert not case.target_attempts and case.history.tip == base, "a failing member check moved main"
    assert not case.store.owner_merge_facts()
    retirement, = batch.retirements(case.ctx)
    assert retirement["reason"].startswith("member #1"), retirement["reason"]
    ran = owner_runs(marks)
    assert ran.get("test_tip_conflict", 0) == 1, ran
    assert ran.get("test_own", 0) == 1 and ran.get("test_own_1", 0) == 1, ran
    assert ran.get("test_ci_covered", 0) == 0, f"owner re-ran a test the tip's CI passed: {ran}"
    tip = retirement["candidate"]
    assert_one_timed_pass(case, {"literal": [batch._name(base, 2), "#5", "#1"], "shas": [tip]},
                          wall, advanced)


def test_single_admission_skips_tests_ci_passed_on_the_exact_refreshed_commit(
        proto, monkeypatch, tmp_path):
    case = proto
    marks = tmp_path / "owner-marks.txt"
    update_base(case, small_repo_files(marks))
    pytest_ = f"{PYTHON} -m pytest -q -p no:cacheprovider"
    set_done_when(case, 5, [f"{pytest_} {OWN.format(n=5)} tests/test_ci_covered.py "
                            "tests/test_not_in_ci.py tests/test_machine_only.py"])
    support.queue_workers(case, [5])
    member = case.tracker.pr_head_sha(support.pr_for(case, 5).number)
    marks.unlink(missing_ok=True)
    case.history.update({"src/neighbor.py": "VALUE = 'moved main'\n"})
    moved = case.history.tip
    refreshed, lookups = [], []

    def receipt(head):
        lookups.append(head)
        try:
            parents = git_command(case.root, "rev-list", "--parents", "-n", "1", head).split()[1:]
        except Exception:
            return None
        if head != member and member in parents and moved in parents:
            refreshed.append(head)
            return 42, {CI_COVERED, MACHINE, f"{OWN.format(n=5)}::test_own"}
        if head == member:  # Evidence for another commit than the one checked.
            return 7, {CI_COVERED, NOT_IN_CI, MACHINE}
        return None

    monkeypatch.setattr(case.tracker, "pr_unit_passed_tests", receipt)
    wall, advanced = owner_pass(case, monkeypatch)  # Check the refreshed commit on moved main.
    support.drain(case, 1)
    assert len(case.tracker.merged_prs) == 1
    assert git_command(case.history.origin, "show", "main:src/member_5.py") == "VALUE = 5"

    # Count through landing too, so a later tick cannot quietly repeat checks.
    ran = owner_runs(marks)
    assert ran.get("test_ci_covered", 0) == 0, f"owner re-ran a test CI passed on the checked commit: {ran}"
    assert ran.get("test_not_in_ci", 0) == 1, ran
    assert ran.get("test_own", 0) == 1 and ran.get("test_machine_only", 0) == 1, ran
    assert refreshed, f"the owner never asked for the refreshed commit's passing set: {lookups}"
    assert_one_timed_pass(case, {"literal": ["#5"], "shas": refreshed}, wall, advanced)
