"""Shared offline helpers for unit tests."""

from copy import deepcopy
import json
import subprocess
from urllib.parse import urlsplit


REPO = "owner/repo"
RUN = 40082146744
_RUN_URL = f"https://github.com/{REPO}/actions/runs/{RUN}"
_ANNOTATION = (
    "The job was not started because recent account payments have failed or your "
    "spending limit needs to be increased. Please check the 'Billing & plans' "
    "section in your settings"
)
_FAILED_STEPS = [
    {"name": "Set up job", "number": 1, "status": "completed", "conclusion": "success"},
    {"name": "Run tests", "number": 2, "status": "completed", "conclusion": "failure"},
]


def job(number=6001, name="test", *, steps=(), annotation=_ANNOTATION, conclusion="failure"):
    return {
        "id": number, "databaseId": number, "run_id": RUN, "name": name,
        "status": "completed", "conclusion": conclusion, "steps": deepcopy(list(steps)),
        "html_url": f"{_RUN_URL}/job/{number}", "url": f"{_RUN_URL}/job/{number}",
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
        self.jobs = [job(steps=[{**step, "conclusion": "success"} for step in _FAILED_STEPS],
                         annotation=None, conclusion="success")]

    def command(self, args, input=None):
        self.calls.append(list(args))
        head = self.case.tracker.pr_head_sha(2)
        jobs = [{key: value for key, value in item.items() if key != "annotation"}
                for item in self.jobs]
        run = {
            "id": RUN, "databaseId": RUN, "name": "ci", "status": "completed",
            "conclusion": "success" if self.passing else "failure",
            "head_sha": head, "headSha": head, "html_url": _RUN_URL, "url": _RUN_URL,
            "jobs_url": f"https://api.github.com/repos/{REPO}/actions/runs/{RUN}/jobs",
            "jobs": jobs,
        }
        if args[:2] == ["pr", "checks"]:
            checks = [{"name": "ci", "bucket": "pass" if self.passing else "fail",
                       "link": _RUN_URL, "workflow": "ci"}]
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
