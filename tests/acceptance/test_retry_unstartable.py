"""Acceptance test for retry-unstartable. Locked by Foragent; do not edit.

`fa orch retry N` must not report success for an issue the gate can never start.
Park removes `approved` on purpose and a hash mismatch removes it too, while a start
needs both approval and readiness. So retrying a parked issue fails and names the park
and its reason; retrying an open issue without approval fails and says it needs a new
approval. Either refusal leaves the issue's open PR open, adds no label and queues no
retry. (A retry the gate can start keeps today's behaviour, guarded by PR CI.)

Runs through the public CLI with offline fakes (FakeTracker, local Git history).
"""

import json
import re

import pytest

HINT = "Use the existing locale selection"
PARK_WHY = "Waiting for the storage redesign in order 41"


def blocked_case(git_history, tmp_path, monkeypatch):
    from foragent.actions import flag
    from foragent.reasons import Reason
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    case.store.finish_run(1, outcome="blocked")
    flag(case.ctx, 1, Reason.REVIEW_DISPUTE, "Locale mismatch", "Raw details")
    case.tracker.remove_labels(1, ["running"])
    main = bind_cli(case, monkeypatch)
    monkeypatch.setattr("foragent.cli.RunContext", lambda **kwargs: case.ctx)
    pr = case.tracker.create_pr(head="foragent/issue-1", base="main", title="Old attempt", body="Old")
    other = case.tracker.create_pr(head="other/work", base="main", title="Other", body="Other")
    return case, main, [pr, other]


def facts(case, prs):
    """Every effect a retry can have: labels, PR states, comments, journal, hint and retry marker."""
    journal = case.store.root / "orch-events.jsonl"
    hints = case.store.root / "hints"
    return {
        "labels": sorted(case.tracker.get_issue(1).labels),
        "prs": {pr: case.tracker.get_issue(pr).state for pr in prs},
        "comments": [c.body for c in case.tracker.list_comments(1)],
        "events": [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else [],
        "hints": sorted((p.name, p.read_bytes()) for p in hints.rglob("*") if p.is_file()) if hints.exists() else [],
        "outbox": list(case.store.github_outbox()),
    }


def retry(case, main, capsys, hint):
    capsys.readouterr()
    code = main(["orch", "retry", "1", *(["--hint", hint] if hint else [])])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def assert_refused_without_effects(case, prs, before, code, output):
    assert code != 0, f"A retry the gate cannot start must exit with a failure: {output!r}"
    assert "queued implementation retry" not in output, f"A refused retry must not report a queued retry: {output!r}"
    after = facts(case, prs)
    assert after["prs"] == before["prs"], "A refused retry leaves the open PR open (and every other PR as it was)"
    assert all(state == "open" for state in after["prs"].values())
    assert after["labels"] == before["labels"], "A refused retry adds no label (and removes none)"
    assert "ready" not in after["labels"]
    assert after["hints"] == before["hints"], "A refused retry queues no retry marker or hint"
    assert after["comments"] == before["comments"], "A refused retry posts no retry comment"
    assert after["outbox"] == before["outbox"], "A refused retry queues no GitHub write"
    assert not any(e.get("kind") == "retry" for e in after["events"][len(before["events"]):]), (
        "A refused retry journals no queued retry")


def names_park(text):
    return re.search(r"\bpark|보류", text, re.IGNORECASE) is not None


def names_new_approval(text):
    return re.search(r"(?:new|fresh)\s+approval|re-?approv|approv\w*\s+again|새(?:로운)?\s*승인|다시\s*승인|재승인",
                     text, re.IGNORECASE) is not None


@pytest.mark.parametrize("hint", [None, HINT], ids=["no-hint", "hint"])
def test_retry_of_parked_issue_fails_names_park_and_reason_and_keeps_pr(
        git_history, tmp_path, monkeypatch, capsys, hint):
    case, main, prs = blocked_case(git_history, tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["orch", "park", "1", "--why", PARK_WHY]) == 0, "Parking through the public verb must succeed"
    assert "approved" not in case.tracker.get_issue(1).labels, "Park removes approval on purpose"
    before = facts(case, prs)

    code, output = retry(case, main, capsys, hint)

    assert names_park(output), f"The refusal must say the issue is parked: {output!r}"
    assert PARK_WHY in " ".join(output.split()), f"The refusal must name the park reason: {output!r}"
    assert_refused_without_effects(case, prs, before, code, output)


@pytest.mark.parametrize("hint", [None, HINT], ids=["no-hint", "hint"])
def test_retry_of_unapproved_issue_fails_says_new_approval_needed_and_keeps_pr(
        git_history, tmp_path, monkeypatch, capsys, hint):
    case, main, prs = blocked_case(git_history, tmp_path, monkeypatch)
    # A hash mismatch (or any lost approval) leaves an open, unparked issue without `approved`.
    case.tracker.remove_labels(1, ["approved"])
    assert case.tracker.get_issue(1).state == "open"
    before = facts(case, prs)

    code, output = retry(case, main, capsys, hint)

    assert names_new_approval(output), f"The refusal must say the issue needs a new approval: {output!r}"
    assert not names_park(output), f"An unparked issue must not be reported as parked: {output!r}"
    assert_refused_without_effects(case, prs, before, code, output)
