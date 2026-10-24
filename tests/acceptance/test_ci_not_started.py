"""Acceptance test for ci-not-started. Locked by Foragent; do not edit."""

from copy import deepcopy
import json
import os
import signal
import subprocess
from urllib.parse import urlsplit

import pytest

from test_ci_feedback import LOG, make_case
from test_orch_review_handoff import bind_cli
from test_skills_autosync import isolated_home
from test_state_split import offline_acceptance


REPO = "owner/repo"
RUN = 40082146744
RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN}"
HOLD = "ci-not-started"
ANNOTATION = (
    "The job was not started because recent account payments have failed or your "
    "spending limit needs to be increased. Please check the 'Billing & plans' "
    "section in your settings"
)
PROVISIONING = "A runner could not be provisioned."
FAILED_STEPS = [
    {"name": "Set up job", "number": 1, "status": "completed", "conclusion": "success"},
    {"name": "Run tests", "number": 2, "status": "completed", "conclusion": "failure"},
]


@pytest.fixture(autouse=True)
def no_signals(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CI acceptance tests must not send real signals")

    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(signal, "raise_signal", forbidden)


def job(number=6001, name="test", *, steps=(), annotation=ANNOTATION, conclusion="failure"):
    return {
        "id": number, "databaseId": number, "run_id": RUN, "name": name,
        "status": "completed", "conclusion": conclusion, "steps": deepcopy(list(steps)),
        "html_url": f"{RUN_URL}/job/{number}", "url": f"{RUN_URL}/job/{number}",
        "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{number + 1000}",
        # Fixture-only metadata is removed before returning a GitHub job response.
        "annotation": annotation,
    }


class RecordedCI:
    """Real GhTracker reads, recorded Actions responses, FakeTracker writes.

    Nothing here classifies failures. Empty or unavailable logs and job/annotation responses
    reproduce the evidence that today's pr_failed_logs loses. Support both the
    Actions REST jobs endpoint and gh run view --json jobs without fixing query
    order or requiring a new production API.
    """

    def __init__(self, case, monkeypatch, jobs, logs=""):
        from foragent.tracker import GhTracker

        self.case, self.jobs, self.logs = case, deepcopy(jobs), logs
        self.calls = []
        self.passing = False
        gh = GhTracker(REPO, self.command)
        monkeypatch.setattr(case.tracker, "pr_checks", gh.pr_checks)
        monkeypatch.setattr(case.tracker, "pr_failed_logs", gh.pr_failed_logs)

    def succeed(self):
        self.passing = True
        self.logs = ""
        self.jobs = [job(steps=[{**step, "conclusion": "success"} for step in FAILED_STEPS],
                         annotation=None, conclusion="success")]

    def command(self, args, input=None):
        self.calls.append(list(args))
        head = self.case.tracker.pr_head_sha(2)
        jobs = [{key: value for key, value in item.items() if key != "annotation"}
                for item in self.jobs]
        run = {
            "id": RUN, "databaseId": RUN, "name": "ci", "status": "completed",
            "conclusion": "success" if self.passing else "failure",
            "head_sha": head, "headSha": head, "html_url": RUN_URL, "url": RUN_URL,
            "jobs_url": f"https://api.github.com/repos/{REPO}/actions/runs/{RUN}/jobs",
            "jobs": jobs,
        }
        if args[:2] == ["pr", "checks"]:
            checks = [{"name": "ci", "bucket": "pass" if self.passing else "fail",
                       "link": RUN_URL, "workflow": "ci"}]
            output = json.dumps(checks)
            if not self.passing:
                # Real gh exits 1 even though its stdout contains valid check data.
                raise subprocess.CalledProcessError(1, ["gh", *args], output=output, stderr="")
            return output
        if args[:2] == ["run", "view"]:
            assert args[2] == str(RUN), args
            if "--log-failed" in args:
                if isinstance(self.logs, Exception):
                    raise self.logs
                return self.logs
            assert "--json" in args, f"Unrecorded gh command: {args}"
            fields = args[args.index("--json") + 1].split(",")
            return json.dumps({field: run[field] for field in fields})

        assert args[:1] == ["api"], f"Unrecorded gh command: {args}"
        prefix = f"repos/{REPO}/"
        endpoint = next((arg for arg in args if arg.startswith((prefix, "https://api.github.com/"))), None)
        assert endpoint is not None, f"Unrecorded gh command: {args}"
        path = urlsplit(endpoint).path.removeprefix("/")
        responses = {
            f"{prefix}pulls/2": {"head": {"sha": head}},
            f"{prefix}actions/runs": {"workflow_runs": [run]},
            f"{prefix}actions/runs/{RUN}": run,
            f"{prefix}actions/runs/{RUN}/jobs": {"total_count": len(jobs), "jobs": jobs},
        }
        for item in self.jobs:
            annotations = [] if item["annotation"] is None else [{
                "path": ".github", "start_line": 1, "end_line": 1,
                "annotation_level": "failure", "message": item["annotation"],
            }]
            check_path = f"{prefix}check-runs/{item['id'] + 1000}"
            responses[check_path] = {
                "id": item["id"] + 1000, "name": item["name"],
                "status": item["status"], "conclusion": item["conclusion"],
                "details_url": item["html_url"],
                "output": {"annotations_count": len(annotations)},
            }
            responses[f"{check_path}/annotations"] = annotations
        assert path in responses, f"Unrecorded gh endpoint: {args}"
        response = responses[path]
        if "--jq" in args:
            projection = args[args.index("--jq") + 1]
            assert projection in {".workflow_runs", ".jobs"}, args
            response = response[projection[1:]]
        return json.dumps(response)


def ci_case(git_history, tmp_path, monkeypatch, jobs, logs="", **config):
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], **config)
    ci = RecordedCI(case, monkeypatch, jobs, logs)
    return case, ci


def assert_hold(case, outcome, *, attempts, worker_calls, annotation=ANNOTATION):
    from foragent.reasons import parse_reason

    assert outcome == "held", (
        f"CI jobs that never started must be an infrastructure hold, not a code failure; got {outcome!r}"
    )
    assert len(case.implementer.calls) == worker_calls, "Infrastructure must not be sent to the worker"
    run = case.store.get_run(1)
    assert run.status == "finished" and run.attempts == attempts, "Infrastructure spent a CI fix attempt"
    assert case.store.load_flag(1).reason == HOLD
    comments = [comment.body for comment in case.tracker.list_comments(1)
                if parse_reason(comment.body) == HOLD]
    assert comments, f"The issue needs a public, machine-readable {HOLD} hold reason"
    assert annotation in comments[-1], "The hold detail must quote the full failure annotation"
    assert str(RUN) in comments[-1], "The hold detail must identify the failed Actions run"
    assert "blocked" in case.tracker.get_issue(1).labels
    assert "running" not in case.tracker.get_issue(1).labels
    assert case.tracker.get_issue(1).state == "open"
    assert case.tracker.find_pr("foragent/issue-1") == 2
    assert case.tracker.get_issue(2).state == "open"
    assert case.tracker.merged_prs == []


def test_ci_not_started_is_a_public_round_trippable_reason():
    from foragent.reasons import GROUP, Reason, format_comment, parse_reason

    assert HOLD in {reason.value for reason in Reason}, "Reason must publicly include ci-not-started"
    reason = Reason(HOLD)
    assert reason in GROUP
    assert parse_reason(format_comment(reason, "CI did not start", ANNOTATION, label="blocked")) == reason


def test_ci_failure_evidence_preserves_annotation_and_run_when_logs_are_empty(
    git_history, tmp_path, monkeypatch,
):
    case, ci = ci_case(git_history, tmp_path, monkeypatch, [job()])
    assert case.ctx.tracker.pr_checks(2) == "fail"
    detail = case.ctx.tracker.pr_failed_logs(2)
    assert ANNOTATION in detail, "Empty Actions logs must not discard the failure annotation"
    assert str(RUN) in detail, "Failure evidence must name the Actions run"
    assert "test" in detail, "Failure evidence must identify the job"
    assert ci.calls


@pytest.mark.parametrize("jobs,annotation", [
    pytest.param([job()], ANNOTATION, id="billing-zero-steps"),
    pytest.param([job(annotation=PROVISIONING)], PROVISIONING, id="zero-steps-without-not-started-phrase"),
    pytest.param([job(steps=[{"name": "Run tests", "number": 1, "status": "queued", "conclusion": None}])],
                 ANNOTATION, id="annotation-with-step-metadata"),
    pytest.param([job(), job(6002, "lint"),
                  job(6003, "build", steps=FAILED_STEPS[:1], annotation=None, conclusion="success")],
                 ANNOTATION, id="all-failed-jobs-not-started-with-successful-job"),
])
def test_only_not_started_failures_hold_without_worker_or_ci_fix_attempt(
    git_history, tmp_path, monkeypatch, jobs, annotation,
):
    from foragent.runner import run_issue

    case, _ = ci_case(git_history, tmp_path, monkeypatch, jobs, max_retries=1)
    # Let main finish its incorrect retry so the failure reports the contract,
    # rather than exhausting the scripted executor with an unrelated exception.
    case.implementer.steps = [case.implement, case.implement]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    outcome = run_issue(1, case.ctx)
    assert_hold(case, outcome, attempts=1, worker_calls=1, annotation=annotation)


def test_not_started_job_with_missing_logs_holds_without_worker_or_ci_fix_attempt(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    # Observed on #163 and #197: gh cannot fetch a log for a job that never ran.
    # Raise the real subprocess error at the command seam, leaving GhTracker's
    # exception conversion and the runner's error/hold handling under test.
    missing_logs = subprocess.CalledProcessError(
        1, ["gh", "run", "view", str(RUN), "-R", REPO, "--log-failed"],
        output="", stderr="log not found: 120838880611",
    )
    case, _ = ci_case(git_history, tmp_path, monkeypatch, [job(120838880611)],
                      logs=missing_logs, max_retries=1)
    outcome = run_issue(1, case.ctx)
    assert_hold(case, outcome, attempts=1, worker_calls=1)
    recorded = case.store.read_history()[-1]
    assert recorded.outcome == "held", "Missing logs must not finish as an error in runs.jsonl"
    assert recorded.attempts == 1, "Missing logs must not consume a CI fix attempt"


def test_mixed_failure_repairs_real_failed_step_and_preserves_both_jobs(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    case, ci = ci_case(git_history, tmp_path, monkeypatch,
                       [job(), job(6002, "unit-tests", steps=FAILED_STEPS, annotation=None)],
                       "setup ok\n" + LOG + "\n")

    # Same repair/merge scenario as test_ci_feedback, with one additional job
    # that never started in the very same run (not merely a second failed run).
    def fix(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done, CI fixed\n")
        ci.succeed()
        return "Fixed CI"

    case.implementer.steps = [case.implement, fix]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    assert run_issue(1, case.ctx) == "merged", "A real failed step must win over infrastructure evidence"
    assert len(case.implementer.calls) == 2
    prompt = case.implementer.calls[1]["prompt"]
    assert LOG in prompt
    assert ANNOTATION in prompt, "Mixed CI feedback must mention the not-started job as well as the code failure"
    assert str(RUN) in prompt and "unit-tests" in prompt, "Mixed CI detail must name the run and real failed job"
    assert case.store.get_run(1).attempts == 2
    assert case.tracker.merged_prs == [2]
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}


def test_normal_code_feedback_then_infrastructure_hold_can_recheck_and_merge_same_head(
    git_history, tmp_path, monkeypatch,
):
    from conftest import git_command
    from foragent.runner import run_issue

    case, ci = ci_case(git_history, tmp_path, monkeypatch,
                       [job(steps=FAILED_STEPS, annotation=None)], "setup ok\n" + LOG + "\n",
                       max_retries=1)

    def fix(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done, CI fixed\n")
        ci.jobs, ci.logs = [job()], ""
        return "Fixed the failed test; the new CI job could not start"

    case.implementer.steps = [case.implement, fix]
    case.reviewer.steps = ["VERDICT: PASS"] * 4
    outcome = run_issue(1, case.ctx)
    # D50's normal failure still reaches the implementer exactly once and with
    # its original log. Pair this regression with a new hold so every test is red
    # on main, including this test of unchanged code-failure feedback.
    assert len(case.implementer.calls) == 2
    assert LOG in case.implementer.calls[1]["prompt"]
    assert_hold(case, outcome, attempts=2, worker_calls=2)
    head = case.tracker.pr_head_sha(2)
    assert head == git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-1")

    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    # A still-red public recheck must also hold without implementation. This
    # exercises the review-only CI loop, in addition to the publication loop.
    assert main(["orch", "rerun-review", "1"]) == 0
    assert_hold(case, case.store.get_run(1).outcome, attempts=1, worker_calls=2)
    assert case.tracker.pr_head_sha(2) == head

    ci.succeed()
    assert main(["orch", "rerun-review", "1"]) == 0
    assert case.store.get_run(1).outcome == "merged", "A same-head green recheck must clear the hold"
    assert case.tracker.merged_prs == [2]
    assert case.tracker.pr_head_sha(2) == head
    assert git_command(case.history.origin, "rev-parse", "refs/heads/foragent/issue-1") == head
    assert len(case.tracker.created_prs) == 1, "Rechecking must reuse the open PR"
    assert len(case.implementer.calls) == 2, "Recovery must not require another implementation turn"
    assert case.tracker.get_issue(1).state == "closed"
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human", "ready", "running"}
