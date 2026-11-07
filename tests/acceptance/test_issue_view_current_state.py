"""Acceptance test for issue-view-current-state (UX r1111, r923). Locked by Foragent; do not edit.

`fa dashboard N --once` must say what the issue is doing now, the state the main dashboard
shows for it, not the last run's outcome. Before this issue a held issue whose order edit was
re-approved after the hold (queued in the main view, #2482) still read 'waiting for a person'
('사람 확인을 기다리며 멈췄어요'), and a merged issue still read 'handed to the merge owner'
('병합 담당에게 넘겼어요').

Real CLI, gate tick and local state of the shared status world; only GitHub and the clock are
fake. The words checked are the current-state words of each language, not one fixed sentence.
"""

from datetime import timedelta
import json
import re

import pytest

HELD = 5
MERGED = 6

QUEUED_WORDS = {
    "en": re.compile(r"\bqueued\b|\bin (the )?(queue|line)\b|\bits turn\b|\bwait(s|ing)? (for )?its turn\b", re.I),
    "ko": re.compile(r"대기|순서|차례"),
}
PERSON_WORDS = {
    "en": re.compile(r"\bwait(s|ing)? for (a person|a human|you|your)\b|\bneeds? (a person|you)\b"
                     r"|\bheld for a person\b", re.I),
    "ko": re.compile(r"사람 확인을 기다리며 멈췄어요|사람을 기다려요|사람 확인을 기다려요|확인을 기다리고 있어요"),
}
HOLD_HISTORY = {
    "en": re.compile(r"\bheld\b", re.I),
    "ko": re.compile(r"멈췄"),
}
# A line that names the past (the last run, an earlier review, history) starts the history
# part of the view; everything above it is the issue's current state.
HISTORY_LABEL = {
    "en": re.compile(r"\b(last|previous|earlier|past|prior)\b|\bhistory\b|\bended\b|\bbefore this\b", re.I),
    "ko": re.compile(r"마지막|이전|지난|이력|기록|과거"),
}
MERGED_WORDS = {
    "en": re.compile(r"\bmerged\b", re.I),
    "ko": re.compile(r"병합됐|병합되었|병합했|병합 완료|병합을 마쳤"),
}
HANDED_WORDS = {
    "en": re.compile(r"merge owner", re.I),
    "ko": re.compile(r"병합 담당"),
}


def _run(main, argv):
    try:
        return main(argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1


def _world(git_history, tmp_path, monkeypatch, language):
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    case.store.update_run(1, phase="implementing",
                          phase_started_at=(case.clock[0] - timedelta(seconds=90)).isoformat())
    return case, main


def _tick(case):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    case.clock[0] += timedelta(minutes=10)  # the gate's issue refresh is due
    tick(tick_context(case, start_runs=False))


def _show(main, capsys, number):
    capsys.readouterr()
    code = _run(main, ["dashboard", str(number), "--once"])
    captured = capsys.readouterr()
    assert code == 0, f"`fa dashboard {number} --once` must print the issue's view: {captured.err}"
    assert f"#{number}" in captured.out
    return captured.out


def _status_lanes(main, capsys):
    capsys.readouterr()
    assert _run(main, ["gate", "status", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["lanes"]
    return list(rows.values()) if isinstance(rows, dict) else rows


def _current(out, language):
    """The part of the view above the first line that labels history."""
    lines = out.splitlines()
    for index, line in enumerate(lines):
        if HISTORY_LABEL[language].search(line):
            return "\n".join(lines[:index])
    return out


def _numbers(items):
    return {item["number"] if isinstance(item, dict) else item for item in items}


def _held_then_reapproved(case):
    """#5 was held for a person; then its order was edited and re-approved after the hold.

    The hold is GitHub's routing (needs-human label + marker comment) over a run whose recorded
    outcome is held. The person edits the order body and re-approves it; the gate records the
    re-approval, and once the hold label is gone the main dashboard shows it as queued.
    """
    body = case.tracker.get_issue(1).body
    case.tracker.add_issue(HELD, "Edited after hold", body, labels=["lane:docs"])
    case.tracker.record_label(HELD, "approved", "human")
    case.store.start_run(HELD, branch=f"foragent/issue-{HELD}",
                         worktree=case.store.worktree_path(HELD), lane="docs")
    case.clock[0] += timedelta(minutes=1)
    case.store.finish_run(HELD, outcome="held", detail="The review kept failing.")
    case.store.save_flag(HELD, "review-failed", flagged_at=case.clock[0].isoformat())
    case.tracker.add_labels(HELD, ["needs-human"])
    case.tracker.comment(HELD, "<!-- foragent:needs-human reason=review-failed -->\n"
                               "**Needs a decision**\n\nThe review kept failing.")
    case.clock[0] += timedelta(minutes=5)
    case.tracker.edit_body(HELD, body + "\nSmaller scope after the hold.\n")
    case.clock[0] += timedelta(minutes=1)
    case.tracker.remove_labels(HELD, ["approved"])
    case.tracker.record_label(HELD, "approved", "human")


def _merged(case):
    """#6's last run handed its PR to the merge owner, which then merged it and closed the issue."""
    case.tracker.add_issue(MERGED, "Ship the docs page", case.tracker.get_issue(1).body,
                           labels=["approved", "lane:docs"])
    branch = f"foragent/issue-{MERGED}"
    case.store.start_run(MERGED, branch=branch, worktree=case.store.worktree_path(MERGED), lane="docs")
    pr = case.tracker.create_pr(head=branch, base="main", title="Ship the docs page",
                                body=f"Closes #{MERGED}")
    case.store.finish_run(MERGED, outcome="merge-queued", detail="Handed to the merge owner.")
    case.clock[0] += timedelta(minutes=2)
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.tracker.close_issue(MERGED, reason="completed")
    return pr


@pytest.mark.parametrize("language", ["en", "ko"])
def test_reapproved_held_issue_reads_queued_not_waiting_for_a_person(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _held_then_reapproved(case)
    _tick(case)
    approval = case.store.load_approval(HELD)
    assert approval is not None and approval.approved_at > case.store.load_flag(HELD).flagged_at, (
        "Setup: the gate recorded the re-approval given after the hold")
    # The hold is released on GitHub (how is #2599's); the next tick queues the issue.
    case.tracker.remove_labels(HELD, ["needs-human"])
    _tick(case)

    lanes = _status_lanes(main, capsys)
    queued = set().union(*(_numbers(row.get("queued") or []) for row in lanes))
    held = set().union(*(_numbers(row.get("held") or []) for row in lanes))
    assert HELD in queued and HELD not in held, (
        f"Setup: the main dashboard shows #{HELD} as queued: {lanes}")

    out = _show(main, capsys, HELD)
    now = _current(out, language)
    assert QUEUED_WORDS[language].search(now), (
        f"The single-issue view must state, above any history, that #{HELD} is queued:\n{out}")
    assert not PERSON_WORDS[language].search(now), (
        f"#{HELD} is queued now; its current state must not say it waits for a person "
        f"(the held run may appear only under a history label):\n{out}")
    found = QUEUED_WORDS[language].search(out)
    old = HOLD_HISTORY[language].search(out)
    assert old is None or old.start() > found.start(), (
        f"The current state (queued) leads; the held run is only history under it:\n{out}")


@pytest.mark.parametrize("language", ["en", "ko"])
def test_merged_issue_reads_merged_not_handed_to_the_merge_owner(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _merged(case)
    _tick(case)
    row = next(row for row in case.store.local_issues() if row.get("number") == MERGED)
    assert row.get("state") == "closed", f"Setup: the gate observed #{MERGED} closed after the merge: {row}"

    out = _show(main, capsys, MERGED)
    merged = MERGED_WORDS[language].search(out)
    assert merged, f"#{MERGED} is merged; the single-issue view must state it:\n{out}"
    handed = HANDED_WORDS[language].search(out)
    assert handed is None or handed.start() > merged.start(), (
        f"The current state (merged) leads; the hand-off to the merge owner is only history:\n{out}")
