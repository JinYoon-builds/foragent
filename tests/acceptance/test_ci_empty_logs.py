"""Locked acceptance for ci-empty-logs: real runner and GhTracker, fake GitHub I/O."""

from copy import deepcopy
import json
import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import GitHistory, git_command
from foragent.ci_workflow import WORKFLOW
from foragent.runner import run_issue
from foragent.tracker import GhTracker
from test_ci_feedback import make_case
from test_ci_not_started import ANNOTATION, FAILED_STEPS, RUN, assert_hold
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401


REPO = "owner/repo"
MEASUREMENT = RUN + 1
JOB = 910
SECOND_JOB = 911
FAILED_TESTS = (
    "tests/test_widget.py::test_preserves_lane",
    "tests/test_widget.py::test_retries_with_evidence",
)
LOST = "tests/acceptance/test_payload.py::test_missing_from_head"
JOB_ANNOTATION = "AssertionError: the lane must be preserved"
SECOND_ANNOTATION = "Comparison step reported lost test IDs"
EXCLUDED = "EXCLUDED_MEASUREMENT_EVIDENCE"
LOG_HEAD = "OLD_LOG_PREFIX_MUST_NOT_REPLACE_THE_TAIL"


def job(number, name, *, conclusion="failure", steps=None):
    return {
        "id": number, "databaseId": number, "run_id": RUN, "name": name,
        "status": "in_progress" if conclusion is None else "completed",
        "conclusion": conclusion,
        "steps": deepcopy(FAILED_STEPS if steps is None else steps),
        "html_url": f"https://github.com/{REPO}/actions/runs/{RUN}/job/{number}",
        "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{number}",
    }


class GitHubCI:
    """External responses only; failure selection and prompt assembly stay real.

    Run-wide gh logs remain unavailable until completion, including --job, as
    gh run view currently behaves. Completed job REST logs are already readable.
    The ordinary job named acceptance must count; the trusted measurement run
    with the same job display name must stay excluded by workflow provenance.
    """

    def __init__(self, case, monkeypatch, *, completed, fault=None,
                 not_started=False, second_failure=False):
        self.case, self.completed, self.fault = case, completed, fault
        self.passing = False
        self.calls = []
        self.sleeps = []
        self.jobs = [
            job(JOB, "test (1)", steps=[] if not_started else FAILED_STEPS),
            job(912, "test (2)", conclusion="success" if completed else None),
            job(913, "collect", conclusion="success"),
        ]
        if second_failure:
            self.jobs.insert(1, job(SECOND_JOB, "acceptance (0)"))
        self.annotations = {
            JOB: ANNOTATION if not_started else JOB_ANNOTATION,
            SECOND_JOB: SECOND_ANNOTATION,
        }
        self.logs = {
            JOB: LOG_HEAD + "\n" + "setup chatter\n" * 1200
                 + "\n".join(f"FAILED {name} - AssertionError" for name in FAILED_TESTS),
            SECOND_JOB: f"Compare acceptance passing sets\nLost test IDs:\n{LOST}\n",
        }
        reader = GhTracker(REPO, self.command)
        monkeypatch.setattr(case.tracker, "pr_checks", reader.pr_checks)
        monkeypatch.setattr(case.tracker, "pr_failed_logs", reader.pr_failed_logs)
        sleep = case.ctx.sleep

        def observed_sleep(seconds):
            self.sleeps.append(seconds)
            sleep(seconds)

        case.ctx.sleep = observed_sleep

    def fail(self, message):
        raise subprocess.CalledProcessError(1, ["gh"], output="", stderr=message)

    def log(self, number):
        assert number in self.logs, "Only completed, failed ordinary jobs have logs"
        if self.fault == "logs-denied":
            self.fail("HTTP 404: job log unavailable")
        return "" if self.fault == "empty-logs" else self.logs[number]

    def command(self, args, input=None):
        self.calls.append(list(args))
        head = self.case.tracker.pr_head_sha(2)
        jobs = deepcopy(self.jobs)
        if self.passing:
            for item in jobs:
                item.update(status="completed", conclusion="success")
        run = {
            "id": RUN, "databaseId": RUN, "name": "ci", "path": ".github/workflows/ci.yml",
            "status": "completed" if self.completed or self.passing else "in_progress",
            "conclusion": "success" if self.passing else "failure" if self.completed else None,
            "head_sha": head, "headSha": head, "check_suite_id": RUN,
            "html_url": f"https://github.com/{REPO}/actions/runs/{RUN}",
            "repository": {"full_name": REPO}, "jobs": jobs,
        }
        measurement_job = {
            **job(999, "test (1)"), "run_id": MEASUREMENT,
            "html_url": f"https://github.com/{REPO}/actions/runs/{MEASUREMENT}/job/999",
        }
        measurement = {
            **run, "id": MEASUREMENT, "databaseId": MEASUREMENT,
            "name": "custom measurement label", "path": WORKFLOW,
            "status": "completed", "conclusion": "failure", "check_suite_id": MEASUREMENT,
            "jobs": [measurement_job],
        }
        checks = [
            {"name": item["name"], "workflow": "ci", "link": item["html_url"],
             "bucket": "fail" if item["conclusion"] == "failure" else
                       "pending" if item["conclusion"] is None else "pass"}
            for item in jobs
        ]
        checks.append({"name": "test (1)", "workflow": "custom measurement label",
                       "bucket": "fail", "link": measurement["jobs"][0]["html_url"]})
        if args[:2] == ["pr", "checks"]:
            output = json.dumps(checks)
            raise subprocess.CalledProcessError(1, ["gh", *args], output=output, stderr="")
        if args[:2] == ["run", "view"]:
            number = int(args[args.index("--job") + 1]) if "--job" in args else None
            assert number is not None or args[2] == str(RUN), args
            if "--log-failed" in args or "--log" in args:
                if run["status"] != "completed":
                    self.fail(f"run {RUN} is still in progress; logs will be available when it is complete")
                return self.log(number) if number else "\n".join(
                    self.log(item["id"]) for item in jobs if item["conclusion"] == "failure"
                )
            if self.fault == "jobs-denied":
                self.fail("HTTP 403: job metadata unavailable")
            fields = args[args.index("--json") + 1].split(",")
            return json.dumps({field: run[field] for field in fields})

        assert args[0] == "api", f"Unrecorded GitHub command: {args}"
        prefix = f"repos/{REPO}/"
        endpoint = next(arg for arg in args if arg.startswith((prefix, "https://api.github.com/")))
        parsed = urlsplit(endpoint)
        path, query = parsed.path.lstrip("/"), parse_qs(parsed.query)
        responses = {
            f"{prefix}pulls/2": {"head": {"sha": head}},
            f"{prefix}pulls/2/files": [{"filename": "feature.txt"}],
            f"{prefix}actions/runs/{RUN}": run,
            f"{prefix}actions/runs/{RUN}/jobs": {"jobs": jobs, "total_count": len(jobs)},
        }
        if path == f"{prefix}actions/runs":
            runs = [run, measurement]
            if "check_suite_id" in query:
                runs = [r for r in runs if str(r["check_suite_id"]) == query["check_suite_id"][0]]
            elif self.fault == "no-runs":
                runs = []
            response = {"workflow_runs": runs}
        elif path == f"{prefix}commits/{head}/check-runs":
            response = {"check_runs": [
                {"id": item["id"], "name": item["name"], "details_url": item["html_url"],
                 "app": {"slug": "github-actions"},
                 "check_suite": {"id": MEASUREMENT if item["id"] == 999 else RUN}}
                for item in [*jobs, *measurement["jobs"]]
            ]}
        elif path == f"{prefix}actions/runs/{RUN}/jobs" and self.fault == "jobs-denied":
            self.fail("HTTP 403: job metadata unavailable")
        elif path.startswith(f"{prefix}actions/runs/{MEASUREMENT}"):
            pytest.fail("Excluded measurement run must not supply failure evidence")
        elif path.startswith(f"{prefix}actions/jobs/"):
            number = int(path.split("/")[-2] if path.endswith("/logs") else path.split("/")[-1])
            if path.endswith("/logs"):
                return self.log(number)
            response = next(item for item in jobs if item["id"] == number)
        elif path.startswith(f"{prefix}check-runs/") and path.endswith("/annotations"):
            number = int(path.split("/")[-2])
            assert number != 999, EXCLUDED
            if self.fault == "annotations-denied":
                self.fail("HTTP 403: annotations unavailable")
            response = [{"message": self.annotations[number]}]
        else:
            assert path in responses, f"Unrecorded GitHub endpoint: {args}"
            response = responses[path]
        if "--jq" in args:
            projection = args[args.index("--jq") + 1]
            assert projection in {".workflow_runs", ".jobs", ".check_runs", ".[].filename"}, args
            if projection == ".[].filename":
                return "\n".join(item["filename"] for item in response)
            response = response[projection[1:]]
        return json.dumps(response)


def scenario(git_history, tmp_path, monkeypatch, *, completed, **ci_options):
    # Independent disposable histories let one test pair today's completed-run
    # control with the newly required in-progress behavior. No repository writes.
    root = tmp_path / ("completed" if completed else "in-progress")
    root.mkdir()
    origin = shutil.copytree(git_history.origin, root / "origin.git")
    clone = shutil.copytree(git_history.clone, root / "clone")
    git_command(clone, "config", "remote.origin.url", str(origin))
    history = GitHistory(origin, clone, git_history.tip)
    case = make_case(history, root, monkeypatch, skill_libraries=[], max_retries=1)
    ci = GitHubCI(case, monkeypatch, completed=completed, **ci_options)
    return case, ci


def retry(case, ci):
    def repair(kwargs):
        (kwargs["worktree"] / "feature.txt").write_text("done, CI fixed\n")
        ci.passing = True
        return "Repaired CI"

    case.implementer.steps = [case.implement, repair]
    case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
    outcome = run_issue(1, case.ctx)
    assert outcome == "merged", f"Readable or unavailable CI evidence must reach the retry: {outcome}"
    assert len(case.implementer.calls) == 2
    assert case.store.get_run(1).attempts == 2
    assert case.tracker.merged_prs == [2]
    assert ci.sleeps == [], "A known red job must not wait for its pending siblings"
    prompt = case.implementer.calls[1]["prompt"]
    assert "CI failed:" in prompt
    assert EXCLUDED not in prompt
    return prompt


def test_completed_and_in_progress_runs_deliver_each_failed_jobs_annotation_and_tail(
    git_history, tmp_path, monkeypatch,
):
    for completed in (True, False):
        case, ci = scenario(git_history, tmp_path, monkeypatch,
                            completed=completed, second_failure=True)
        prompt = retry(case, ci)
        for expected in ("test (1)", "acceptance (0)", JOB_ANNOTATION, SECOND_ANNOTATION,
                         *FAILED_TESTS, LOST):
            assert expected in prompt, f"Missing failed-job evidence with completed={completed}: {expected}"
        assert LOG_HEAD not in prompt, "Keep the useful log tails within today's bounded feedback"


@pytest.mark.parametrize("fault", [
    "logs-denied", "empty-logs", "annotations-denied", "jobs-denied", "no-runs",
])
def test_unreadable_evidence_is_explicit_in_the_retry_and_readable_parts_survive(
    git_history, tmp_path, monkeypatch, fault,
):
    case, ci = scenario(git_history, tmp_path, monkeypatch, completed=False, fault=fault)
    prompt = retry(case, ci)
    assert "unavailable" in prompt.lower(), "Say that CI evidence is unavailable instead of a blank body"
    if fault not in {"jobs-denied", "no-runs"}:
        assert "test (1)" in prompt
    if fault in {"logs-denied", "empty-logs"}:
        assert JOB_ANNOTATION in prompt, "Unavailable logs must not discard a readable annotation"
    if fault == "annotations-denied":
        assert all(name in prompt for name in FAILED_TESTS), "Unavailable annotations must not discard readable logs"


def test_not_started_still_holds_without_a_retry_even_with_pending_siblings(
    git_history, tmp_path, monkeypatch,
):
    for completed in (True, False):
        case, ci = scenario(git_history, tmp_path, monkeypatch,
                            completed=completed, not_started=True)
        # Let today's blind retry reach a normal outcome so executor exhaustion
        # cannot masquerade as the missing infrastructure hold.
        case.implementer.steps = [case.implement, case.implement]
        case.reviewer.steps = ["VERDICT: PASS", "VERDICT: PASS"]
        assert_hold(case, run_issue(1, case.ctx), attempts=1, worker_calls=1)
        assert ci.sleeps == []
        assert not any("--log-failed" in args or "--log" in args or
                       any(arg.endswith("/logs") for arg in args) for args in ci.calls), (
            "A job that never started needs its annotation, not a log read"
        )
