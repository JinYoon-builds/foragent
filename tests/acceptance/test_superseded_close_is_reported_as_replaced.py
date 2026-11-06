"""Acceptance test for superseded-close-is-reported-as-replaced.

Finding (r840): `fa orch close N --superseded-by M` closed #1760 as completed, and when its
order closed, the order's comment said the order was applied and listed #1760 as completed,
although #1760 never merged and #1970 replaced it.

Public contract:
- `fa orch close N --superseded-by M` closes N as not planned (not completed); its closing
  comment still names #M.
- When an order closes, its closing comment counts such a child as replaced and names it with
  its replacement (for example "replaced: #1760 -> #1970"), not as completed or cancelled. An
  order none of whose children merged is not described as applied.
- A child that merged is still reported as completed, as today, in both en and ko.

Real CLI (`fa orch close`) and the real gate tick that closes an order record; the tracker is
the project's fake tracker, Git is the existing offline history fixture.
"""

from dataclasses import replace
import json
import re

import pytest

from foragent.scheduler import tick
from test_orch_close_superseded import close, superseded_case
from test_orch_review_handoff import tick_context

SUPERSEDED, REPLACEMENT, MERGED, ORDER = 1, 2, 3, 10

WORDS = {
    # language: (word for "replaced", words that must not describe a superseded child or an unmerged order)
    "en": ("replaced", ("applied", "completed", "cancel")),
    "ko": ("대체", ("적용", "완료", "취소")),
}
COMPLETED_TODAY = {"en": "Completed: #{n}.", "ko": "완료: #{n}."}


def in_language(case, language):
    case.ctx.config = replace(case.ctx.config, language=language)
    return case


def add_order(case, children):
    """An order record as `fa order publish` leaves it: plan marker, no lane label, native sub-issues."""
    mapping = {f"child-{index}": child for index, child in enumerate(children)}
    marker = f"<!-- foragent:plan id=order-{ORDER} map={json.dumps(mapping)} acceptance={{}} -->"
    case.tracker.add_issue(ORDER, "Order", "Ship the order.\n\n" + marker)
    for child in children:
        case.tracker.add_sub_issue(ORDER, child)


def gate_tick(case):
    report = tick(tick_context(case, start_runs=False))
    assert report.skipped is None, f"An offline gate tick must run: {report}"


def order_comment(case):
    order = case.tracker.get_issue(ORDER)
    assert order.state == "closed", "The order must close once every child has ended"
    comments = case.tracker.list_comments(ORDER)
    assert len(comments) == 1, f"Closing the order leaves exactly one comment: {[c.body for c in comments]}"
    return comments[0].body


def names_replacement(body, old, new):
    """`#old` is named and followed by `#new` with no other issue number between them."""
    return re.search(rf"#{old}(?!\d)[^#]*#{new}(?!\d)", body) is not None


@pytest.mark.parametrize("language", ["en", "ko"])
def test_superseded_child_closes_as_not_planned_naming_its_replacement(
        git_history, tmp_path, monkeypatch, language):
    """Reject a close that keeps GitHub's default (completed), one that drops the replacement
    from the comment, and one that comments twice."""
    case = in_language(superseded_case(git_history, tmp_path, monkeypatch), language)

    assert close(case) == 0, "fa orch close --superseded-by must succeed for a blocked, unmerged issue"

    issue = case.tracker.get_issue(SUPERSEDED)
    assert issue.state == "closed"
    assert issue.state_reason == "not_planned", (
        f"A superseded issue never shipped; it must close as not planned, not {issue.state_reason!r}"
    )
    added = case.tracker.list_comments(SUPERSEDED)[case.before["comments"]:]
    assert len(added) == 1, f"Exactly one closing comment: {[c.body for c in added]}"
    assert re.search(rf"#{REPLACEMENT}(?!\d)", added[0].body), (
        f"The closing comment must name the replacing issue #{REPLACEMENT}: {added[0].body!r}"
    )
    assert case.tracker.get_issue(REPLACEMENT).state == "open", "The replacing issue is not touched"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_order_whose_only_child_was_superseded_reports_it_replaced(
        git_history, tmp_path, monkeypatch, language):
    """Reject an order comment that lists the superseded child as completed or cancelled, one
    that says the order was applied, one that omits the replacement, and a doubled comment."""
    case = in_language(superseded_case(git_history, tmp_path, monkeypatch), language)
    add_order(case, [SUPERSEDED])

    assert close(case) == 0
    gate_tick(case)

    body = order_comment(case)
    replaced, forbidden = WORDS[language]
    assert names_replacement(body, SUPERSEDED, REPLACEMENT), (
        f"The order comment must name #{SUPERSEDED} with its replacement #{REPLACEMENT}: {body!r}"
    )
    assert replaced in body.casefold(), f"The order comment must count #{SUPERSEDED} as replaced: {body!r}"
    for word in forbidden:
        assert word not in body.casefold(), (
            f"No child merged: the order comment must not say {word!r} (the owner reads that it shipped): {body!r}"
        )


@pytest.mark.parametrize("language", ["en", "ko"])
def test_order_with_merged_and_superseded_children_reports_each_as_it_ended(
        git_history, tmp_path, monkeypatch, language):
    """Reject a comment that folds the superseded child into the completed group, one that drops
    the merged child's completed line (reported as today), and one without the replacement."""
    case = in_language(superseded_case(git_history, tmp_path, monkeypatch), language)
    add_order(case, [SUPERSEDED, MERGED])  # #3 merged earlier (the fixture's merged run, closed).

    assert close(case) == 0
    gate_tick(case)

    body = order_comment(case)
    completed = COMPLETED_TODAY[language].format(n=MERGED)
    assert completed in body, (
        f"The merged child is reported as completed, exactly as today ({completed!r}): {body!r}"
    )
    assert names_replacement(body, SUPERSEDED, REPLACEMENT), (
        f"The order comment must name #{SUPERSEDED} with its replacement #{REPLACEMENT}: {body!r}"
    )
    assert WORDS[language][0] in body.casefold(), f"#{SUPERSEDED} must be counted as replaced: {body!r}"
