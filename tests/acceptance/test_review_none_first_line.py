"""Locked acceptance for review-none-first; publish under tests/acceptance/.

A reviewer that states "no findings" with the "(none)" marker means the same
thing whether the marker is the first or the last line of its reply. Before
this order, a native /code-review reply that put "(none)" first and a prose
note after it became an unusable review with findings, so the implementer was
sent back three times and the run ended as retries-exceeded (#1801).

Break-it: a first-line marker treated as findings (rework), any path:line
finding next to the marker dropped (false pass), and prose without a marker
accepted as a pass must each fail a test here.
"""

import json

import pytest

from foragent.run_review import parse_code_review
from foragent.runner import run_issue
from test_review_verdict_line import (
    assert_no_findings_round, ordinary_case, response, review_events,
)
from test_order_machine_budget import fake_workloads  # noqa: F401
from test_review_perspectives import review_offline  # noqa: F401
from test_risk_map import offline  # noqa: F401
from test_skills_autosync import isolated_home  # noqa: F401


# The reply #1801 got three times on 2026-11-04 (shortened, same shape).
PROSE = ("The change is only in `src/foragent/presence.py`. I found no runtime bug "
         "visible in the hunks. The new branch keeps the old return value.")
NONE_FIRST = "(none)\n\n" + PROSE
NONE_LAST = PROSE + "\n\n(none)"
FINDING = "feature.txt:1 — the retry path loses the selected locale"


def event(text):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text})


def scripted(case, first, rounds=1):
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        return response(kwargs, first if len(calls) <= rounds else "VERDICT: PASS")

    case.reviewer.run = review
    return calls


@pytest.mark.parametrize("reviewer", ["claude", "codex"])
def test_none_on_the_first_line_with_a_prose_note_is_no_findings(
    git_history, tmp_path, monkeypatch, reviewer,
):
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer)
    calls = scripted(case, NONE_FIRST)
    outcome = run_issue(1, case.ctx)
    # Same route as the locked last-line form: no implementation rework, no
    # defect hold, the round stays visible, and the run merges.
    assert_no_findings_round(case, outcome)
    assert 1 <= len(calls) <= 2
    assert not any(PROSE in call["prompt"] for call in case.implementer.calls), (
        "A prose note beside the no-findings marker is not a finding to fix"
    )


def test_first_and_last_line_markers_parse_the_same():
    first, last = parse_code_review(event(NONE_FIRST)), parse_code_review(event(NONE_LAST))
    assert (first.verdict, first.findings, first.no_findings) == (
        last.verdict, last.findings, last.no_findings), (first, last)
    assert last.no_findings and not first.findings


@pytest.mark.parametrize("reviewer", ["claude", "codex"])
@pytest.mark.parametrize("raw", [
    "(none)\n\n" + FINDING,
    "(none)\n\n" + PROSE + "\n" + FINDING,
    FINDING + "\n\n(none)",
], ids=["marker-then-finding", "marker-prose-finding", "finding-then-marker"])
def test_a_path_line_finding_beside_the_marker_is_not_a_pass(
    git_history, tmp_path, monkeypatch, reviewer, raw,
):
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer)
    case.implementer.steps = [case.implement] * 2
    calls = scripted(case, raw)
    outcome = run_issue(1, case.ctx)
    assert len(case.implementer.calls) == 2, "The finding must go back to the implementer"
    assert "feature.txt:1" in case.implementer.calls[1]["prompt"]
    assert outcome == "merged" and len(calls) == 2
    assert review_events(case)[0]["verdict"] != "PASS"


@pytest.mark.parametrize("reviewer", ["claude", "codex"])
def test_prose_without_marker_or_verdict_stays_unusable(
    git_history, tmp_path, monkeypatch, reviewer,
):
    case = ordinary_case(git_history, tmp_path, monkeypatch, reviewer)
    case.implementer.steps = [case.implement] * 2
    scripted(case, PROSE)
    outcome = run_issue(1, case.ctx)
    assert len(case.implementer.calls) == 2, "Prose with no marker and no verdict is not a pass"
    assert "A reviewer said (verbatim):\n" + PROSE in case.implementer.calls[1]["prompt"]
    assert outcome == "merged"
    events = review_events(case)
    assert events[0]["verdict"] == "UNUSABLE" and events[0]["findings"] == 0
    assert parse_code_review(event(PROSE)).verdict == "UNUSABLE"
