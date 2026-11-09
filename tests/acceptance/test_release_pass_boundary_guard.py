"""Regression controls for r1288 (not in Done when: these already pass on main).

An orchestrator release is a boundary only for the same approved body and only with
review_pass_scope on; a reviewer PASS stays a boundary as it is today. Break-it: a release
that narrows the review after the setting is off, or after the approved body changed, and
a change that drops today's reviewer PASS boundary each fail here.
"""

import pytest

from foragent.issue import body_hash
from foragent.runner import run_issue
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_release_pass_boundary import (RELEASES, Reviewer, content, held_world, release, retry,
                                        write)
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401
from types import SimpleNamespace

from test_orch_review_handoff import make_case


def assert_whole(reviewer, outcome):
    assert reviewer.after, f"The retry must reach another review; outcome {outcome!r}"
    context = reviewer.after[0]
    assert "+kept = 'reviewed'" in context and "+accepted = 'reviewed'" in context, (
        "Without a usable boundary the review keeps the whole change")
    assert not any("Earlier PASS boundary" in text for text in reviewer.after)


@pytest.mark.parametrize("kind", RELEASES)
def test_setting_off_keeps_the_whole_review_after_a_release(git_history, tmp_path, monkeypatch, kind):
    case, reviewer, request, pr, head = held_world(git_history, tmp_path, monkeypatch, review_pass_scope=False)
    release(case, monkeypatch, reviewer, kind, request, pr, head)
    outcome = retry(case, write(content(retry=2)))
    assert_whole(reviewer, outcome)
    assert all(head not in context for context in reviewer.after)


@pytest.mark.parametrize("kind", RELEASES)
def test_body_edited_after_a_release_restores_the_whole_review(git_history, tmp_path, monkeypatch, kind):
    case, reviewer, request, pr, head = held_world(git_history, tmp_path, monkeypatch)
    release(case, monkeypatch, reviewer, kind, request, pr, head)
    approval = case.store.load_approval(1)
    body = approval.body.replace("Implement the feature.", "Keep the saved result reliable too.")
    case.tracker.edit_body(1, body)
    case.store.save_approval(**{**approval.__dict__, "body": body, "body_sha": body_hash(body)})
    outcome = retry(case, write(content(retry=2)))
    assert_whole(reviewer, outcome)
    assert "Keep the saved result reliable too." in reviewer.after[0]


def test_reviewer_pass_stays_a_boundary(git_history, tmp_path, monkeypatch):
    git_history.update({"feature.txt": content("initial", 0)})
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     language="en", review_max=5, max_retries=5)
    reviewer = Reviewer()
    reviewer.released = True  # every review PASSes
    fake = SimpleNamespace(run=reviewer.run)
    case.ctx.executor_factory = lambda name: case.implementer if name == case.config.executor else fake
    case.tracker.set_pr_checks(2, "fail")
    case.tracker.set_pr_failed_logs(2, "offline CI requires retry repair")
    passed = []

    def repair(call):
        passed.append(case.tracker.pr_head_sha(2))
        case.tracker.set_pr_checks(2, "pass")
        return write(content(retry=2))(call)

    case.implementer.steps = [write(content(), kept="kept = 'reviewed'\n"), repair]
    assert run_issue(1, case.ctx) == "merged"
    assert len(reviewer.after) == 2 and len(passed) == 1
    later = reviewer.after[1]
    assert passed[0] in later
    assert "+kept = 'reviewed'" not in later and "+accepted = 'reviewed'" not in later
    assert "-retry = 1" in later and "+retry = 2" in later
