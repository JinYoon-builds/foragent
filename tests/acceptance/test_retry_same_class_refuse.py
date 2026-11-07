"""Acceptance test for retry-same-class-refuse. Locked by Foragent; do not edit.

When the gate already held an issue for two failed rounds of the same defect class,
another `fa orch retry N` (with or without --hint) only repeats the failure. The retry
refuses, changes nothing, and says to narrow, split or redesign the Goal through an
order edit.

The hold is reached through the real runner with fake agents (the same path as
test_review_decision_hold_reader).
"""

from datetime import timedelta
import json
import re

import pytest

from foragent import orch_events
from foragent.reasons import Reason
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_orch_review_handoff import bind_cli
from test_review_decision_hold_reader import pending_review_decision
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


HINT = "Persist the saved result before the reply is sent."


def held_case(git_history, tmp_path, monkeypatch):
    """The gate's two-round same-class hold, with an open PR of the issue and a bound CLI."""
    case, _script, _question = pending_review_decision(git_history, tmp_path, monkeypatch)
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS
    pr = case.tracker.find_pr("foragent/issue-1")
    if pr is None:
        pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Attempt", body="Attempt")
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    case.clock[0] += timedelta(minutes=1)
    return case, main, pr


def retry_args(hint):
    return ["orch", "retry", "1", *(["--hint", hint] if hint else [])]


def effects(case, pr):
    """Everything a retry may change; a refusal must leave all of it as it was."""
    root = case.store.root
    issue = case.tracker.get_issue(1)
    return {
        "labels": sorted(issue.labels),
        "pr_state": case.tracker.get_issue(pr).state,
        "comments": [c.body for c in case.tracker.list_comments(1)],
        "retry_events": orch_events.events(case.store, "retry"),
        "hint": (root / "hints/1.txt").read_text() if (root / "hints/1.txt").exists() else None,
        "state_files": sorted(str(p.relative_to(root)) for p in root.rglob("*")
                              if p.is_file() and "retry" in p.name),
        "outbox": json.dumps(case.store.github_outbox(), sort_keys=True, default=str),
    }


def says_how_to_proceed(text):
    words = " ".join(text.casefold().split())
    return ("order edit" in words and "goal" in words
            and all(re.search(rf"\b{word}", words) for word in ("narrow", "split", "redesign")))


@pytest.mark.parametrize("hint", [None, HINT], ids=["no-hint", "hint"])
def test_retry_of_a_same_class_hold_is_refused_with_the_way_forward(
        git_history, tmp_path, monkeypatch, capsys, hint):
    case, main, pr = held_case(git_history, tmp_path, monkeypatch)
    capsys.readouterr()

    code = main(retry_args(hint))
    out = capsys.readouterr()

    assert code not in (0, None), "A third try of the same defect class must exit with a failure"
    assert says_how_to_proceed(out.out + out.err), (
        f"The refusal must say to narrow, split or redesign the Goal through an order edit: {out!r}")
    assert "queued implementation retry" not in out.out + out.err


@pytest.mark.parametrize("hint", [None, HINT], ids=["no-hint", "hint"])
def test_refused_retry_changes_nothing(git_history, tmp_path, monkeypatch, capsys, kicks, hint):
    case, main, pr = held_case(git_history, tmp_path, monkeypatch)
    before = effects(case, pr)
    kicked = list(kicks)

    code = main(retry_args(hint))

    assert code not in (0, None), "The retry must be refused"
    after = effects(case, pr)
    assert after["pr_state"] == "open", "A refused retry must not close the issue's PR"
    assert "ready" not in after["labels"], "A refused retry must not queue the issue"
    assert after == before, "A refused retry must change nothing"
    assert kicks == kicked, "A refused retry must not kick a gate tick"
    assert case.store.load_flag(1).reason == Reason.REVIEW_DEFECTS, "The hold stays as it was"
