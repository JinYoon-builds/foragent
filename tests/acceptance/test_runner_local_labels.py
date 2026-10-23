"""Acceptance test for runner-local-labels. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
import shlex
import subprocess
from urllib.parse import unquote, urlsplit

from conftest import ACCEPTANCE
from foragent import cli, runner, scheduler
from foragent.issue import body_hash
from foragent.state import StateStore
from test_local_view import github_reply
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance

RUN = 5  # The issue the gate's runner process works on.
EARLIER = 1  # An approved issue the same tick visits before RUN.
DEAD = 7  # A genuinely dead runner.
DEAD_PID = 987654321


def local_row(case, number):
    """Read a fresh store instance so no in-memory cache can answer."""
    rows = {row["number"]: row for row in StateStore(case.store.root).local_issues()}
    return rows[number]


def history(case, number):
    """Persisted rows too: read_history() may hide duplicates."""
    raw = [json.loads(line) for line in
           (case.store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    return [(row["status"], row["outcome"]) for row in raw if row["number"] == number]


def gate_case(git_history, tmp_path, monkeypatch, *, dead=False):
    """Seed #RUN as the gate leaves it right after launching `fa gate run-issue`.

    The runner goes through the real CLI and real GhTracker; GitHub replies
    are faked at the gh transport, backed by the same FakeTracker the gate
    tick reads, so both processes see one GitHub.
    """
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     max_retries=0, max_runtime_minutes=90, stall_minutes=5)
    # #1 is a plain approved issue waiting for its turn, not a running one.
    case.store.finish_run(EARLIER, outcome="spec-question")
    case.tracker.remove_labels(EARLIER, ["running"])
    case.tracker.record_label(EARLIER, "approved", "human")
    body = case.tracker.get_issue(EARLIER).body
    case.tracker.add_issue(RUN, "Runner issue", body, labels=["running"])
    case.tracker.record_label(RUN, "approved", "human")
    approval = dict(body=body, body_sha=body_hash(body),
                    acceptance_blobs=case.git.blob_shas("origin/main", [ACCEPTANCE]),
                    approved_by="human", approved_at=case.clock[0].isoformat())
    case.store.save_approval(RUN, **approval)
    case.store.start_run(RUN, branch=f"foragent/issue-{RUN}", worktree=case.store.worktree_path(RUN))
    if dead:
        case.tracker.add_issue(DEAD, "Dead runner issue", body, labels=["running"])
        case.tracker.record_label(DEAD, "approved", "human")
        case.store.save_approval(DEAD, **approval)
        case.store.start_run(DEAD, branch=f"foragent/issue-{DEAD}",
                             worktree=case.store.worktree_path(DEAD), pid=DEAD_PID)
        kill = scheduler.os.kill
        case.probes = []

        def process_gone(pid, sig):
            if pid == DEAD_PID:
                assert sig == 0
                case.probes.append(pid)
                raise ProcessLookupError(pid)
            return kill(pid, sig)

        monkeypatch.setattr(scheduler.os, "kill", process_gone)
    # The gate's snapshot shows #RUN running, as at launch.
    scheduler.tick(tick_context(case, start_runs=False))
    assert "running" in local_row(case, RUN)["labels"]

    def gh_run(argv, input=None):
        assert argv[0] == "api", f"Unexpected gh command: {argv}"
        if "-X" not in argv or argv[argv.index("-X") + 1] == "GET":
            if argv[1] == "graphql":
                number = next(int(arg.removeprefix("number=")) for arg in argv
                              if arg.startswith("number="))
                return json.dumps({"data": {"repository": {"issue": {
                    "lastEditedAt": case.tracker.body_last_edited_at(number)}}}})
            return github_reply(case, argv)
        verb = argv[argv.index("-X") + 1]
        endpoint = next(arg for arg in argv if arg.startswith("repos/owner/repo/"))
        parts = urlsplit(endpoint).path.removeprefix("repos/owner/repo/").split("/")
        assert parts[0] == "issues", f"Unexpected write endpoint: {argv}"
        number = int(parts[1])
        if parts[2:] == ["labels"] and verb == "POST":
            case.tracker.add_labels(number, [arg.removeprefix("labels[]=") for arg in argv
                                             if arg.startswith("labels[]=")])
            return "[]"
        if parts[2:3] == ["labels"] and verb == "DELETE":
            case.tracker.remove_labels(number, [unquote(parts[3])])
            return "[]"
        assert parts[2:] == ["comments"] and verb == "POST", argv
        return json.dumps({"id": case.tracker.comment(number, json.loads(input)["body"])})

    from foragent.tracker import GhTracker

    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr(cli, "GhTracker", lambda repo: GhTracker(repo, run=gh_run))
    real_context = cli.RunContext

    def run_context(*args, **kwargs):
        # Only the worker processes are offline; the CLI builds everything else.
        return real_context(*args, executor_factory=case.ctx.executor_factory,
                            sleep=case.ctx.sleep, monotonic=case.ctx.monotonic, **kwargs)

    monkeypatch.setattr(cli, "RunContext", run_context)
    case.kicks = []
    monkeypatch.setattr(runner, "kick_tick", lambda root: case.kicks.append(root))
    run_logged = runner.run_logged

    def check_process(argv, *, cwd, timeout_s, log_path, **kwargs):
        words = shlex.split(argv[2]) if argv[:2] == ["/bin/sh", "-c"] else []
        report = next((word.split("=", 1)[1] for word in words if word.startswith("--junitxml=")), None)
        if report is not None:
            from pathlib import Path

            Path(report).write_text("<testsuite/>\n", encoding="utf-8")
            return 0, False, ""
        return run_logged(argv, cwd=cwd, timeout_s=timeout_s, log_path=log_path, **kwargs)

    monkeypatch.setattr(runner, "run_logged", check_process)

    def work(kwargs):
        # The evidence run: the session ran out of its runtime budget.
        case.clock[0] += timedelta(minutes=case.config.max_runtime_minutes)
        return "Worker stopped"

    case.implementer.steps = [work]
    case.started_at = case.store.get_run(RUN).started_at

    def run_cli():
        assert main(["gate", "run-issue", str(RUN), "--repo-root", str(case.root)]) == 0, (
            case.store.log_path(RUN).read_text())
        # Contract 1: every label change is local as soon as the command returns.
        row = local_row(case, RUN)
        case.after_cli = {"local": sorted(set(row["labels"]) & {"running", "blocked", "ready"}),
                          "github": sorted(case.tracker.get_issue(RUN).labels
                                           & {"running", "blocked", "ready"}),
                          "run": (case.store.get_run(RUN).status, case.store.get_run(RUN).outcome)}

    case.run_cli = run_cli
    return case


def tick_finishing_runner_midway(case):
    """A gate tick that read its issue list before the runner process ended.

    Real ticks take minutes on a busy board (evidence: #163 was recovered at
    16:35:02 and the same tick started #184 at 16:36:17), so the runner's
    final label writes land while an earlier-started tick is still visiting
    other issues. The tick visits #EARLIER first; the runner finishes there.
    """
    events = case.tracker.label_events
    fired = []

    def label_events(number):
        if number == EARLIER and not fired:
            fired.append(number)
            case.run_cli()
        return events(number)

    context = tick_context(case, start_runs=False)
    case.tracker.label_events = label_events
    try:
        report = scheduler.tick(context)
    finally:
        del case.tracker.label_events
    assert fired == [EARLIER], "The tick never visited the earlier issue"
    return report


def outcome(case, number=RUN):
    return {
        "github": sorted(case.tracker.get_issue(number).labels & {"running", "blocked", "ready"}),
        "local": sorted(set(local_row(case, number)["labels"]) & {"running", "blocked", "ready"}),
        "history": history(case, number),
    }


EXPECTED = {"github": ["blocked"], "local": ["blocked"], "history": [("finished", "timeout")]}


def test_runner_local_labels_cli_run_ending_during_a_tick_stays_blocked(
    git_history, tmp_path, monkeypatch, offline_acceptance,
):
    case = gate_case(git_history, tmp_path, monkeypatch)
    report = tick_finishing_runner_midway(case)
    assert case.after_cli == {"local": ["blocked"], "github": ["blocked"],
                              "run": ("finished", "timeout")}, (
        "`fa gate run-issue` must leave blocked and no running in the local snapshot on return")
    assert case.kicks, "The runner must still kick the gate when it ends"
    observed = outcome(case)
    assert observed == EXPECTED, (
        "A tick that listed the issue before the runner ended must not record a crash or "
        f"return a blocked issue to ready; observed={observed}; actions={report.actions}")


def test_runner_local_labels_later_ticks_keep_the_issue_for_the_orchestrator(
    git_history, tmp_path, monkeypatch, offline_acceptance,
):
    case = gate_case(git_history, tmp_path, monkeypatch)
    tick_finishing_runner_midway(case)
    launched = []
    later = tick_context(case)
    later.launcher = lambda n: launched.append(n) or 7000 + n
    snapshots = []
    for _ in range(2):
        case.clock[0] += timedelta(seconds=30)
        scheduler.tick(later)
        snapshots.append(outcome(case))
    assert RUN not in launched, "A timed-out run must wait for the orchestrator, not restart"
    assert snapshots == [EXPECTED, EXPECTED], (
        "Ticks after the run must keep one timeout row and blocked without ready; "
        f"observed={snapshots}")


def test_runner_local_labels_dead_runner_is_still_recovered_once(
    git_history, tmp_path, monkeypatch, offline_acceptance,
):
    case = gate_case(git_history, tmp_path, monkeypatch, dead=True)
    tick_finishing_runner_midway(case)
    case.clock[0] += timedelta(seconds=1)
    scheduler.tick(tick_context(case, start_runs=False))
    observed = {RUN: outcome(case), DEAD: outcome(case, DEAD)}
    assert case.probes == [DEAD_PID], "The dead runner must be probed and recovered exactly once"
    assert observed == {
        RUN: EXPECTED,
        DEAD: {"github": ["ready"], "local": ["ready"], "history": [("finished", "crashed")]},
    }, ("A dead runner still gets exactly one crashed row and returns to ready, while a runner "
        f"that ended during the tick gets none; observed={observed}")
