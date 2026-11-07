"""Acceptance test for order-report-names-split-replacements.

Finding (r1030): a child split into several issues (#2158 replaced by #2481, #2482, #2483
under order #2480) is reported in its parent order's closing comment as
'취소(계획 안 함): #2158', so the owner reads that the work was dropped.

Public contract:
- `fa orch close N --superseded-by A B C --why TEXT` accepts several issue numbers and the
  closed issue records all of them as its replacements.
- When such a child's order closes, the order's closing comment names the child as replaced
  and lists every replacement issue number, not as cancelled.
- A child closed as not planned with no replacement still reads as cancelled.
- (r1034) When an order's only child was replaced by issues that are still open, the order's
  close note names those replacement issues and does not say the work finished (no
  'applied', 'completed', '(0 ended)' / '적용됐고', '완료', '끝남 0개').
- (r1034) `fa dashboard <child> --once` for a child closed as replaced names its replacement
  issues and no longer shows the stale 'split it in order' next action.

Real CLI (`fa orch close`) and the real gate tick that closes an order record; the tracker is
the project's fake tracker, Git is the existing offline history fixture.
"""

import json
import re

import pytest

from foragent.actions import flag
from foragent.reasons import Reason
from test_orch_close_superseded import WHY, superseded_case
from test_skills_autosync import cli_exit
from test_superseded_close_is_reported_as_replaced import (
    add_order, gate_tick, in_language, order_comment,
)

# Other numbers stay above the fixture's issues and its PR, so no fixture object is overwritten.
SPLIT = 1  # The fixture's blocked, unmerged child with its own locked test and open PR.
REPLACEMENTS = (31, 32, 33)  # Three issues of another order replace it.
OTHER_ORDER = 30
DROPPED = 37  # Closed as not planned with no replacement.

CANCELLED_GROUP = {"en": "Cancelled (not planned):", "ko": "취소(계획 안 함):"}
WORDS = {
    # language: (word for "replaced", word for "cancelled")
    "en": ("replaced", "cancel"),
    "ko": ("대체", "취소"),
}


def add_other_order(case):
    """The order the replacement issues belong to: published, its children still open."""
    mapping = {f"part-{n}": n for n in REPLACEMENTS}
    marker = f"<!-- foragent:plan id=order-{OTHER_ORDER} map={json.dumps(mapping)} acceptance={{}} -->"
    case.tracker.add_issue(OTHER_ORDER, "Split order", "Do the split work.\n\n" + marker)
    for n in REPLACEMENTS:
        case.tracker.add_issue(n, f"Split part {n}", "One part of the split work.")
        case.tracker.add_sub_issue(OTHER_ORDER, n)


# Words that tell the owner the order's work finished (r1034: '이 주문은 적용됐고 … 끝남 0개').
FINISHED = {
    "en": (r"\bapplied\b", r"\bcompleted?\b", r"\d+ ended\b", r"\bevery sub-issue ended\b"),
    "ko": (r"적용됐", r"적용되었", r"완료", r"끝남"),
}
TOO_LARGE = "Too big for one session; split it in order"


def split_case(git_history, tmp_path, monkeypatch, language, *, running=False):
    case = in_language(superseded_case(git_history, tmp_path, monkeypatch, running=running), language)
    add_other_order(case)
    case.before_replacements = {n: len(case.tracker.list_comments(n)) for n in REPLACEMENTS}
    return case


def close_split(case):
    return cli_exit(case.main, ["orch", "close", str(SPLIT), "--superseded-by",
                                *(str(n) for n in REPLACEMENTS), "--why", WHY])


def mentions(body, n):
    return len(re.findall(rf"#{n}(?!\d)", body))


def replacements_named_after(body, old):
    """Issue numbers that directly follow `#old`, skipping the replacements' order number."""
    numbers = [int(n) for n in re.findall(r"#(\d+)", body)]
    assert old in numbers, f"#{old} must be named: {body!r}"
    following = []
    for n in numbers[numbers.index(old) + 1:]:
        if n not in (*REPLACEMENTS, OTHER_ORDER):
            break
        following.append(n)
    return following


def cancelled_group(body, language):
    """The text of the cancelled group, from its heading to the end of its sentence ('' if absent)."""
    heading = CANCELLED_GROUP[language]
    if heading not in body:
        return ""
    return body.split(heading, 1)[1].split(".", 1)[0]


@pytest.mark.parametrize("language", ["en", "ko"])
def test_close_records_every_replacement_of_a_split_issue(git_history, tmp_path, monkeypatch, language):
    """Reject a CLI that refuses several numbers, one that keeps only the first or last, a close
    that keeps GitHub's default (completed), and a repeated close that comments again."""
    case = split_case(git_history, tmp_path, monkeypatch, language)

    assert close_split(case) == 0, (
        "fa orch close --superseded-by must accept several issue numbers for one closed issue"
    )

    issue = case.tracker.get_issue(SPLIT)
    assert issue.state == "closed"
    assert issue.state_reason == "not_planned", (
        f"Split work never shipped; it must close as not planned, not {issue.state_reason!r}"
    )
    added = case.tracker.list_comments(SPLIT)[case.before["comments"]:]
    assert len(added) == 1, f"Exactly one closing comment: {[c.body for c in added]}"
    for n in REPLACEMENTS:
        assert mentions(added[0].body, n) == 1, (
            f"The closing comment must name replacement #{n} exactly once: {added[0].body!r}"
        )
    for n in REPLACEMENTS:
        replacement = case.tracker.get_issue(n)
        assert replacement.state == "open", f"Replacement #{n} is not touched"
        assert len(case.tracker.list_comments(n)) == case.before_replacements[n], (
            f"Replacement #{n} gets no comment"
        )

    assert close_split(case) == 0, "Repeating the same close is a successful no-op"
    again = case.tracker.list_comments(SPLIT)[case.before["comments"]:]
    assert len(again) == 1, f"A repeated close must not comment again: {[c.body for c in again]}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_order_names_split_child_with_all_three_replacements(git_history, tmp_path, monkeypatch, language):
    """Reject an order comment that lists the split child as cancelled, one that names only one
    replacement, one that drops the child, and one that names a replacement twice."""
    case = split_case(git_history, tmp_path, monkeypatch, language)
    add_order(case, [SPLIT])

    assert close_split(case) == 0, "fa orch close --superseded-by must accept several issue numbers"
    gate_tick(case)

    body = order_comment(case)
    replaced, cancelled = WORDS[language]
    assert replaced in body.casefold(), f"The order comment must count #{SPLIT} as replaced: {body!r}"
    assert cancelled not in body.casefold(), (
        f"#{SPLIT} was split, not dropped: the order comment must not call it cancelled: {body!r}"
    )
    assert mentions(body, SPLIT) == 1, f"#{SPLIT} is named once: {body!r}"
    following = replacements_named_after(body, SPLIT)
    assert sorted(set(following) - {OTHER_ORDER}) == list(REPLACEMENTS), (
        f"#{SPLIT} must be followed by every replacement {REPLACEMENTS}, as in '#old -> #new': {body!r}"
    )
    for n in REPLACEMENTS:
        assert mentions(body, n) == 1, f"Replacement #{n} is named exactly once: {body!r}"
    assert case.tracker.get_issue(OTHER_ORDER).state == "open", "The replacements' order is still running"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_split_child_is_replaced_while_dropped_child_stays_cancelled(
        git_history, tmp_path, monkeypatch, language):
    """Reject a comment that folds the split child into the cancelled group, one that turns a
    child with no replacement into a replaced one, and one without every replacement."""
    case = split_case(git_history, tmp_path, monkeypatch, language)
    case.tracker.add_issue(DROPPED, "Dropped work", "Nobody does this any more.")
    case.tracker.close_issue(DROPPED, reason="not_planned")
    add_order(case, [SPLIT, DROPPED])

    assert close_split(case) == 0, "fa orch close --superseded-by must accept several issue numbers"
    gate_tick(case)

    body = order_comment(case)
    group = cancelled_group(body, language)
    assert mentions(group, DROPPED) == 1, (
        f"#{DROPPED} closed as not planned with no replacement still reads as cancelled: {body!r}"
    )
    assert mentions(group, SPLIT) == 0, f"#{SPLIT} must not be in the cancelled group: {body!r}"
    assert WORDS[language][0] in body.casefold(), f"#{SPLIT} must be counted as replaced: {body!r}"
    following = replacements_named_after(body, SPLIT)
    assert sorted(set(following) - {OTHER_ORDER}) == list(REPLACEMENTS), (
        f"#{SPLIT} must be followed by every replacement {REPLACEMENTS}: {body!r}"
    )
    assert DROPPED not in following, f"#{DROPPED} has no replacement and must not read as one: {body!r}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_order_whose_only_child_was_split_does_not_say_its_work_finished(
        git_history, tmp_path, monkeypatch, language):
    """r1034: reject a close note that says the order was applied or counts its children as
    ended/completed, one that leaves out a replacement, and one that names it twice."""
    case = split_case(git_history, tmp_path, monkeypatch, language)
    add_order(case, [SPLIT])

    assert close_split(case) == 0, "fa orch close --superseded-by must accept several issue numbers"
    for n in REPLACEMENTS:
        assert case.tracker.get_issue(n).state == "open", f"Replacement #{n} is still open"
    gate_tick(case)

    body = order_comment(case)
    for n in REPLACEMENTS:
        assert mentions(body, n) == 1, (
            f"The close note must name open replacement #{n} exactly once: {body!r}"
        )
    for pattern in FINISHED[language]:
        assert not re.search(pattern, body, re.IGNORECASE), (
            f"Its only child was split into open issues: the close note must not say the work "
            f"finished ({pattern!r}): {body!r}"
        )


def dashboard(case, capsys, n):
    capsys.readouterr()
    code = cli_exit(case.main, ["dashboard", str(n), "--once"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("language", ["en", "ko"])
def test_dashboard_of_split_child_names_its_replacements_not_the_stale_next_action(
        git_history, tmp_path, monkeypatch, capsys, language):
    """r1034: reject an issue view that keeps 'split it in order' on the closed, split child,
    one that names no replacement or only some of them."""
    case = split_case(git_history, tmp_path, monkeypatch, language, running=True)
    # The run ran out of budget as too large, exactly as the runner records it.
    flag(case.ctx, SPLIT, Reason.TOO_LARGE, TOO_LARGE)
    case.store.finish_run(SPLIT, outcome="too-large")
    case.tracker.remove_labels(SPLIT, ["running"])

    code, before, err = dashboard(case, capsys, SPLIT)
    assert code == 0, err
    assert "split it in order" in before, f"Precondition: the too-large next action is shown: {before!r}"
    for n in REPLACEMENTS:
        assert mentions(before, n) == 0, f"Precondition: #{n} is not named yet: {before!r}"

    assert close_split(case) == 0, "fa orch close --superseded-by must accept several issue numbers"

    code, out, err = dashboard(case, capsys, SPLIT)
    assert code == 0, f"`fa dashboard {SPLIT} --once` must show the closed, split issue: {err}"
    assert mentions(out, SPLIT) >= 1, f"The view is about #{SPLIT}: {out!r}"
    for n in REPLACEMENTS:
        assert mentions(out, n) >= 1, f"The view must name replacement #{n}: {out!r}"
    assert "split it in order" not in out.casefold(), (
        f"#{SPLIT} is already split and closed: the stale next action must go: {out!r}"
    )
