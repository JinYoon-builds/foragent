"""Architect r1288: an orchestrator release narrows the next review like a reviewer PASS.

World: the real runner holds issue #1 after two FAIL rounds of the same class (the
review-decision request). The orchestrator then releases the head the hold reviewed,
either by `fa orch resolve 1 --verdict pass` (CI fails, so a retry is queued) or by
answering the review-decision request with a releasing answer and retrying. The next
review of the same approved body must cover only the change since that head, plus the
whole change of pass_scope_exempt paths and the conflict resolution.

Only public entry points run: the runner, the `fa orch` CLI and the inbox answer API that
`fa orch request-status` calls. Reviewers, scorers and implementers are offline fakes.
Break-it: no boundary (today's whole review), a boundary at the issue's base or at the
first FAIL round's head, an exemption ignored, or a conflict resolution dropped each fail
here. Controls that pass on main (setting off, body edited, reviewer PASS) live in
test_release_pass_boundary_guard.py.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import yaml

from conftest import git_command
from foragent import cli, orch_inbox
from foragent.executors import ExecResult
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_orch_review_handoff import bind_cli, make_case
from test_review_blocks_major_only import ASSESSMENT, review_decisions
from test_review_grounded_fail import scoring_request
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import entry, offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


FINDING = "feature.txt:1 — A saved result is wrong.\n  class: result persistence"
RELEASES = ["resolve-pass", "ship with follow-up", "narrow"]


def content(accepted="reviewed", retry=1, base=0):
    # Far-apart lines let Git separate the issue's edit from main's edit in one file.
    return (f"accepted = {accepted!r}\nretry = {retry}\n"
            + "".join(f"padding_{i} = {i}\n" for i in range(21))
            + f"base = {base}\n")


class Reviewer:
    """Every review session FAILs until the release; afterwards it PASSes and is recorded."""

    def __init__(self):
        self.released = False
        self.before, self.after = [], []

    def run(self, **call):
        if scoring_request(call["prompt"]) is not None:
            text = json.dumps(ASSESSMENT)
        else:
            local = call["worktree"] / "CLAUDE.local.md"
            context = call["prompt"] + (local.read_text() if local.is_file() else "")
            (self.after if self.released else self.before).append(context)
            text = "VERDICT: PASS" if self.released else f"VERDICT: FAIL\n{FINDING}"
        if call.get("review_level") is not None:
            text = json.dumps({"result": text})
        return ExecResult(0, False, text, call["log_path"])


def write(feature, *, kept=None):
    def run(call):
        (call["worktree"] / "feature.txt").write_text(feature)
        if kept is not None:
            (call["worktree"] / "kept.txt").write_text(kept)
        return "Implemented"
    return run


def held_world(git_history, tmp_path, monkeypatch, *, exempt=False, **settings):
    """Reach the two-round same-class hold; return the case, the reviewer and the held head."""
    files = {"feature.txt": content("initial", 0)}
    if exempt:
        files[".foragent-risks.yml"] = yaml.safe_dump([entry(["feature.txt"], pass_scope_exempt=True)])
    git_history.update(files)
    case = make_case(git_history, tmp_path, monkeypatch,
                     **{"skill_libraries": [], "test_command": "true", "language": "en",
                        "review_max": 5, "max_retries": 5, **settings})
    reviewer = Reviewer()
    fake = SimpleNamespace(run=reviewer.run)
    case.ctx.executor_factory = lambda name: case.implementer if name == case.config.executor else fake
    # Round 1 reviews a first draft; round 2 (the round the hold reviewed) the accepted code.
    case.implementer.steps = [write(content("first"), kept="kept = 'first'\n"),
                              write(content(), kept="kept = 'reviewed'\n")]
    assert run_issue(1, case.ctx) == "held"
    assert len(case.implementer.calls) == 2
    request, = review_decisions(case)
    pr = case.tracker.created_prs[-1].number
    head = case.tracker.pr_head_sha(pr)
    assert git_command(case.history.origin, "show", f"{head}:feature.txt") == content().strip()
    assert git_command(case.history.origin, "show", f"{head}:kept.txt") == "kept = 'reviewed'"
    return case, reviewer, request, pr, head


def release(case, monkeypatch, reviewer, kind, request, pr, head):
    """The orchestrator releases the held head; the retry is queued for the runner."""
    case.clock[0] += timedelta(minutes=1)
    if kind == "resolve-pass":
        case.tracker.set_pr_checks(pr, "fail")
        case.tracker.set_pr_failed_logs(pr, "CI requires a repair")
        main = bind_cli(case, monkeypatch)
        monkeypatch.setattr(cli, "RunContext", lambda **kwargs: case.ctx)
        for name in ("FORAGENT_ROLE", "FORAGENT_SESSION_ID"):
            monkeypatch.delenv(name, raising=False)
        assert main(["orch", "resolve", "1", "--verdict", "pass", "--note", "Ship with follow-ups"]) == 0
        assert not case.tracker.merged_prs and "ready" in case.tracker.get_issue(1).labels, (
            "Failing CI after the resolve pass must queue an implementation retry")
    else:
        # `fa orch request-status ID done --answer ...` calls this API.
        answered = orch_inbox.set_request_status(case.store, request["id"], "done",
                                                 answer=kind, require_answer=True)
        assert answered["status"] == "done"
        case.tracker.remove_labels(1, ["blocked"])
        case.tracker.add_labels(1, ["running"])
    case.clock[0] += timedelta(minutes=1)
    reviewer.released = True


def retry(case, *steps):
    def ci_green(step):
        def run(call):
            for pr in case.tracker.created_prs:
                case.tracker.set_pr_checks(pr.number, "pass")
            return step(call)
        return run

    case.implementer.steps = [ci_green(step) for step in steps]
    return run_issue(1, case.ctx)


def assert_bounded(reviewer, head, outcome, case, *, since="-retry = 1"):
    assert reviewer.after, f"The retry must reach another review; outcome {outcome!r}"
    for context in reviewer.after:
        assert head in context, "The reviewer must receive the head the orchestrator released"
        assert "+kept = 'reviewed'" not in context, (
            "Code unchanged since the released head is not presented as new work")
        assert "accepted = 'first'" not in context and "kept = 'first'" not in context, (
            "The boundary is the head the hold reviewed, not an earlier round's head")
    assert any(since in context and "+retry = 2" in context for context in reviewer.after), (
        "The change since the released head reaches the reviewer")
    assert outcome == "merged" and len(case.tracker.merged_prs) == 1


@pytest.mark.parametrize("kind", RELEASES)
def test_release_narrows_the_next_review_to_the_change_since_the_released_head(
    git_history, tmp_path, monkeypatch, kind,
):
    case, reviewer, request, pr, head = held_world(git_history, tmp_path, monkeypatch)
    release(case, monkeypatch, reviewer, kind, request, pr, head)
    outcome = retry(case, write(content(retry=2)))
    assert_bounded(reviewer, head, outcome, case)
    for context in reviewer.after:
        assert "+accepted = 'reviewed'" not in context, (
            "Code unchanged since the released head is not presented as new work")


@pytest.mark.parametrize("kind", RELEASES)
def test_release_keeps_exempt_paths_reviewed_whole(git_history, tmp_path, monkeypatch, kind):
    case, reviewer, request, pr, head = held_world(git_history, tmp_path, monkeypatch, exempt=True)
    release(case, monkeypatch, reviewer, kind, request, pr, head)
    outcome = retry(case, write(content(retry=2)))
    # feature.txt is exempt: its diff runs from the issue's base, so its old retry line is `retry = 0`.
    assert_bounded(reviewer, head, outcome, case, since="-retry = 0")
    for context in reviewer.after:
        assert "+accepted = 'reviewed'" in context, (
            "A pass_scope_exempt path keeps its whole change in the review after a release")
        assert "exempt" in context.lower()


@pytest.mark.parametrize("kind", RELEASES)
def test_release_keeps_the_conflict_resolution_in_the_review(git_history, tmp_path, monkeypatch, kind):
    case, reviewer, request, pr, head = held_world(git_history, tmp_path, monkeypatch)
    release(case, monkeypatch, reviewer, kind, request, pr, head)
    # Main moves the issue's own lines after the release: the retry must resolve a real conflict.
    case.history.update({"feature.txt": content("upstream", 0, 1)})
    seen = []

    def resolve(call):
        seen.append("<<<<<<<" in (call["worktree"] / "feature.txt").read_text())
        return write(content(retry=2, base=1))(call)

    outcome = retry(case, resolve, resolve, resolve)
    assert any(seen), "Exercise an actual conflict resolution after the release"
    assert_bounded(reviewer, head, outcome, case)
    assert any("-<<<<<<< " in context for context in reviewer.after), (
        "The conflict resolution itself stays in the review")
