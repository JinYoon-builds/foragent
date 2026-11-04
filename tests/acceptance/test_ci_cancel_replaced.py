"""Acceptance test for ci-cancel-replaced: a new push to a PR cancels the older PR run.

Each push to a Foragent PR starts a full CI round, and the older round keeps
running on a head nobody will merge. On 2026-11-04 that kept 11 Actions runs
queued behind 4 running ones. Now the generated CI workflow cancels the older
pull_request run of the same PR when a new one starts; pushes to main keep
every run. The repository's own ci workflow carries the same rule.

A run cancelled that way belongs to a replaced head. The gate's CI read for the
current head does not count it: only the current head's checks decide. A
cancelled run of the CURRENT head still reads as a failed check, as today.

The workflow goes through the public `fa ci workflow` generator and the
committed files; the CI read goes through GhTracker on a scripted gh whose
answers are scoped by head the way GitHub scopes them.
"""

import json
import re
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from foragent.ci_workflow import WORKFLOW
from foragent.tracker import GhTracker
from test_shard_red import ROOT, generate

REPO = "owner/repo"
OLD, NEW = "a" * 40, "b" * 40


def pr_event(number, head):
    return {"github": {"workflow": "foragent-acceptance", "event_name": "pull_request", "sha": "m" * 40,
                       "event": {"pull_request": {"number": number, "head": {"sha": head}}}}}


def push_event(sha):
    return {"github": {"workflow": "foragent-acceptance", "event_name": "push", "sha": sha, "event": {}}}


EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}")


def operand(text, context):
    """One Actions operand: a quoted string, a context path or a literal."""
    text = text.strip()
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if text in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[text]
    value = context
    for key in text.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def evaluate(expression, context):
    """The Actions subset these rules use: `a || b` yields the first truthy operand; `a == b` compares."""
    for alternative in expression.split("||"):
        if "==" in alternative:
            left, right = alternative.split("==", 1)
            result = operand(left, context) == operand(right, context)
        else:
            result = operand(alternative, context)
        if result:
            return result
    return result


def render(text, context):
    text = str(text)
    whole = EXPRESSION.fullmatch(text.strip())
    if whole and text.count("${{") == 1:
        return evaluate(whole.group(1), context)
    return EXPRESSION.sub(lambda match: str(evaluate(match.group(1), context)), text)


def concurrency(document, context):
    """Render the workflow-level concurrency rule for one event, as Actions would."""
    block = document.get("concurrency")
    assert isinstance(block, dict) and "group" in block, (
        f"The workflow needs a concurrency group so a newer PR run replaces the older one; got {block!r}"
    )
    group = render(block["group"], context)
    cancel = str(render(block.get("cancel-in-progress", False), context)).lower() == "true"
    return group, cancel


def assert_replaces_only_pr_runs(document, name):
    first, cancel_first = concurrency(document, pr_event(7, OLD))
    second, cancel_second = concurrency(document, pr_event(7, NEW))
    other, _ = concurrency(document, pr_event(8, NEW))
    assert first == second and cancel_second, f"{name}: a new push to PR 7 must cancel PR 7's older run"
    assert other != first, f"{name}: another PR's run must not cancel PR 7's run"
    main_a, cancel_a = concurrency(document, push_event("c" * 40))
    main_b, cancel_b = concurrency(document, push_event("d" * 40))
    assert main_a != main_b and not cancel_a and not cancel_b, f"{name}: pushes to main keep every run"


def test_generated_workflow_cancels_the_older_run_of_the_same_pr(tmp_path, monkeypatch):
    document, _ = generate(tmp_path / "workflow", monkeypatch, {"repo": REPO, "checks": "ci"})
    assert_replaces_only_pr_runs(document, WORKFLOW)


def test_committed_workflows_carry_the_rule(tmp_path, monkeypatch):
    settings = yaml.safe_load((ROOT / ".foragent.yml").read_text())
    document, generated = generate(tmp_path / "workflow", monkeypatch, settings)
    assert (ROOT / WORKFLOW).read_text() == generated, "The committed acceptance workflow is the regenerated one"
    assert_replaces_only_pr_runs(document, WORKFLOW)
    ci = ROOT / ".github/workflows/ci.yml"
    assert_replaces_only_pr_runs(yaml.safe_load(ci.read_text()), str(ci.relative_to(ROOT)))


class ReplacedHead:
    """GitHub after a push replaced OLD with NEW on PR 2; OLD's run was cancelled by it.

    `gh pr checks` lists the checks of the PR's current head commit only, and
    Actions runs are listed by head_sha, as GitHub does. `current` is the state of
    NEW's runs: "in_progress", "success" or "cancelled".
    """

    def __init__(self, current):
        self.current = current
        self.calls = []

    def runs(self):
        old = {"id": 11, "name": "ci", "head_sha": OLD, "status": "completed", "conclusion": "cancelled",
               "event": "pull_request", "path": ".github/workflows/ci.yml",
               "html_url": f"https://github.com/{REPO}/actions/runs/11"}
        done = self.current != "in_progress"
        new = {**old, "id": 12, "head_sha": NEW, "status": "completed" if done else "in_progress",
               "conclusion": self.current if done else None,
               "html_url": f"https://github.com/{REPO}/actions/runs/12"}
        return [old, new]

    def jobs(self, run):
        conclusion = run["conclusion"]
        steps = [{"name": "Run tests", "number": 1, "status": "completed" if conclusion else "in_progress",
                  "conclusion": "cancelled" if conclusion == "cancelled" else conclusion}]
        return [{"id": run["id"] * 10, "name": "test (1)", "status": run["status"], "conclusion": conclusion,
                 "steps": steps, "html_url": f"{run['html_url']}/job/{run['id'] * 10}",
                 "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/{run['id'] * 10}"}]

    def __call__(self, args, input=None):
        self.calls.append(list(args))
        if args[:2] == ["pr", "checks"]:
            run = next(run for run in self.runs() if run["head_sha"] == NEW)
            bucket = {"success": "pass", "cancelled": "cancel", None: "pending"}[run["conclusion"]]
            row = {"bucket": bucket, "name": "test (1)", "workflow": "ci", "link": run["html_url"],
                   "state": {"pass": "SUCCESS", "cancel": "CANCELLED", "pending": "IN_PROGRESS"}[bucket],
                   "startedAt": "2026-11-04T04:50:00Z", "completedAt": "0001-01-01T00:00:00Z", "event": "pull_request",
                   "description": ""}
            fields = args[args.index("--json") + 1].split(",")
            output = json.dumps([{field: row[field] for field in fields}])
            if bucket != "pass":
                raise subprocess.CalledProcessError(1 if bucket == "cancel" else 8, ["gh", *args], output=output, stderr="")
            return output
        if args[:2] == ["run", "view"]:
            if "--log-failed" in args or "--log" in args:
                return f"log of run {args[2]}: The operation was canceled.\n"
            run = next(run for run in self.runs() if str(run["id"]) == args[2])
            fields = args[args.index("--json") + 1].split(",")
            return json.dumps({field: {**run, "jobs": self.jobs(run)}.get(field) for field in fields})
        assert args[0] == "api", f"Unrecorded gh command: {args}"
        endpoint = next(arg for arg in args if arg.startswith((f"repos/{REPO}", "https://api.github.com/")))
        parsed = urlsplit(endpoint)
        path, query = parsed.path.lstrip("/").removeprefix("repos/" + REPO + "/"), parse_qs(parsed.query)
        if path == "pulls/2":
            value = {"head": {"sha": NEW}, "number": 2}
        elif path == "actions/runs":
            runs = [run for run in self.runs() if "head_sha" not in query or run["head_sha"] == query["head_sha"][0]]
            value = {"total_count": len(runs), "workflow_runs": runs}
        elif path.startswith("actions/runs/") and path.endswith("/jobs"):
            run = next(run for run in self.runs() if str(run["id"]) == path.split("/")[2])
            value = {"total_count": 1, "jobs": self.jobs(run)}
        elif path.startswith("actions/jobs/") and path.endswith("/logs"):
            return "The operation was canceled.\n"
        elif path.startswith("check-runs/") and path.endswith("/annotations"):
            value = [{"message": "The operation was canceled.", "annotation_level": "failure",
                      "path": ".github", "start_line": 1, "end_line": 1}]
        elif path.startswith("commits/") and path.endswith("/check-runs"):
            sha = path.split("/")[1]
            checks = [{"id": job["id"], "name": job["name"], "head_sha": run["head_sha"], "status": job["status"],
                       "conclusion": job["conclusion"], "app": {"slug": "github-actions"},
                       "details_url": job["html_url"], "check_suite": {"id": run["id"]}}
                      for run in self.runs() if run["head_sha"] == sha for job in self.jobs(run)]
            value = {"total_count": len(checks), "check_runs": checks}
        else:
            pytest.fail(f"Unrecorded GitHub endpoint: {args}")
        if "--jq" in args:
            value = value[args[args.index("--jq") + 1].removeprefix(".")]
        return json.dumps(value)


@pytest.mark.parametrize("current,expected", [("in_progress", "pending"), ("success", "pass")])
def test_cancelled_run_of_a_replaced_head_is_not_a_failure_of_the_current_head(current, expected):
    github = ReplacedHead(current)
    tracker = GhTracker(REPO, github)

    assert tracker.pr_checks(2) == expected, (
        f"Only the current head's checks decide; the older head's cancelled run must not read as failed"
    )
    evidence = tracker.pr_failed_logs(2)
    assert "11" not in str(evidence).split() and "runs/11" not in str(evidence), (
        f"CI failure evidence for the current head must not cite the replaced head's cancelled run: {evidence!r}"
    )
    assert not str(evidence).strip(), f"The current head has no failure: {evidence!r}"


def test_cancelled_run_of_the_current_head_still_reads_as_failed():
    """Today's rule stays: a cancelled check on the head being merged is a failed check."""
    github = ReplacedHead("cancelled")
    tracker = GhTracker(REPO, github)

    assert tracker.pr_checks(2) == "fail"
    assert "runs/12" in str(tracker.pr_failed_logs(2)), "The current head's cancelled run is its failure evidence"
