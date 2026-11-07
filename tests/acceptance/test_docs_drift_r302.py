"""Acceptance tests for docs-drift-r302 (docs owner request r302, 2026-11-06).

Three people-facing doc drifts after #1372 and #1158:
(1) docs/inspect.md row 4: a person reports a try result in a PR comment; body check
    marks are lost when the gate rewrites the body.
(2) docs/gate.md section 5 step 6 (PR): what the gate keeps and rewrites in the PR body.
(3) docs/order.md section 6: the `issues[].replaces` plan field.

These tests read only the public documentation. Code alignment was checked by hand
when the tests were written (see the order's notes), not here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _doc(name):
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


def _section(text, heading):
    start = text.index(heading)
    rest = text[start + len(heading):]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


# --- (1) docs/inspect.md row 4 -------------------------------------------------

def _inspect_rows(number):
    table = _section(_doc("inspect.md"), "## 1.")
    return [line for line in table.splitlines() if re.match(rf"^\|\s*{number}\s*\|", line)]


def _inspect_row_4():
    rows = _inspect_rows(4)
    assert len(rows) == 1, f"docs/inspect.md section 1 has exactly one row 4, found {rows}"
    return rows[0]


def test_inspect_row_4_is_still_the_person_check_row():
    row = _inspect_row_4()
    assert "사람 확인" in row, "Row 4 stays the person-check (사람 확인) group"
    for number in (1, 2, 3, 5, 6):
        assert len(_inspect_rows(number)) == 1, f"Row {number} of the inspect table stays single"


def test_inspect_row_4_says_the_try_result_goes_in_a_pr_comment():
    row = _inspect_row_4()
    assert "PR 댓글" in row, "A person reports the try result in a PR comment (PR 댓글)"
    assert "써 보고" in row, "The person still tries the change (써 보고)"
    assert "써 보고 체크" not in row, (
        "The old action '써 보고 체크' (tick the body box) must be gone: the gate drops body ticks")


def test_inspect_row_4_says_body_check_marks_are_lost_on_rewrite():
    row = _inspect_row_4()
    assert "체크 표시" in row, "Row 4 names the body check marks (체크 표시)"
    assert "다시 쓰" in row, "Row 4 names the gate rewriting the body (다시 쓰면)"
    assert "사라" in row, "Row 4 says the check marks are lost (사라진다)"
    assert row.count("PR 댓글") == 1, "The PR-comment instruction appears once in row 4"


# --- (2) docs/gate.md section 5 step 6 -----------------------------------------

def _gate_section_5():
    return _section(_doc("gate.md"), "## 5. runner")


def _gate_pr_step():
    section = _gate_section_5()
    starts = [m.start() for m in re.finditer(r"(?m)^6\. \*\*PR\.\*\*", section)]
    assert len(starts) == 1, "Section 5 has exactly one step '6. **PR.**'"
    rest = section[starts[0]:]
    following = re.search(r"(?m)^7\. ", rest)
    assert following, "Step 7 still follows the PR step"
    return rest[:following.start()]


def test_gate_pr_step_says_the_gate_writes_the_body_from_recorded_values():
    step = _gate_pr_step()
    assert "기록" in step, "The gate writes the body from recorded values (기록된 값)"
    for kept in ("Closes #N", "요약", "Done when", "체크리스트", "리뷰 판정"):
        assert kept in step, f"The PR step lists '{kept}' among what the body keeps"


def test_gate_pr_step_states_the_body_limit_and_the_comment_overflow():
    step = _gate_pr_step()
    assert "65,536" in step or "65536" in step, "The body limit is 65,536 characters"
    for wrong in ("65,535", "65535", "65,537", "65537"):
        assert wrong not in step, f"The limit is not {wrong}"
    assert "안내 한 줄" in step, "Long optional blocks become one note line (안내 한 줄)"
    assert "PR 댓글" in step, "The full text of a removed block goes in a PR comment"
    assert "쓰지 않" in step and "멈춘다" in step, (
        "If the required content alone is over the limit, the gate writes no body and stops")


def test_gate_pr_step_says_a_person_edited_body_is_not_kept():
    step = _gate_pr_step()
    assert "같은 기록" in step, "An update rewrites from the same record (같은 기록)"
    assert "사람이 고친 본문" in step and "남지 않" in step, (
        "A body a person edited does not survive the rewrite")
    assert step.count("65,536") + step.count("65536") == 1, "The limit is stated once"


def test_gate_section_5_keeps_its_eight_steps_in_order():
    section = _gate_section_5()
    numbers = [int(n) for n in re.findall(r"(?m)^(\d+)\. ", section)]
    assert numbers == list(range(1, 9)), f"Section 5 keeps steps 1-8 once each, found {numbers}"
    assert re.search(r"(?m)^7\. \*\*머지 판정\.\*\*", section), "Step 7 stays the merge decision"


# --- (3) docs/order.md section 6 replaces bullet -------------------------------

def _order_section_6():
    return _section(_doc("order.md"), "## 6. 게시")


def _replaces_bullets():
    bullets = re.split(r"(?m)^- ", _order_section_6())[1:]
    return [b for b in bullets if "replaces" in b]


def _replaces_bullet():
    bullets = _replaces_bullets()
    assert len(bullets) == 1, (
        f"docs/order.md section 6 has exactly one bullet on `issues[].replaces`, found {len(bullets)}")
    return bullets[0]


def test_order_section_6_documents_replaces_closing_on_approval():
    bullet = _replaces_bullet()
    assert "issues[].replaces" in bullet, "The bullet names the plan field `issues[].replaces`"
    assert "대체" in bullet, "The field lists the issue numbers being replaced (대체)"
    assert "승인" in bullet, "Closing happens when the replacing issue is approved (승인)"
    assert "진행하지 않음" in bullet and "닫" in bullet, (
        "The replaced issue is closed as not planned (진행하지 않음으로 닫는다)")
    assert "댓글" in bullet and "후속" in bullet, (
        "A comment links the successor issue (후속 이슈를 잇는 댓글)")


def test_order_section_6_documents_replaces_preview_and_rejections():
    bullet = _replaces_bullet()
    assert "--dry-run" in bullet and "미리" in bullet, (
        "`--dry-run` previews the issues it would close")
    assert "거절" in bullet, "Invalid replace targets are rejected (거절)"
    for target in ("실행 중", "고치거나", "승인하는", "부모", "PR"):
        assert target in bullet, f"The rejected targets include '{target}'"
    assert bullet.count("--dry-run") == 1, "The preview sentence appears once"


def test_order_section_6_keeps_its_existing_bullets():
    section = _order_section_6()
    for kept in ("`edits[]`는 기존 Done when", "`closes`", "fa order edit NUMBER --json"):
        assert kept in section, f"Section 6 keeps its existing bullet on {kept}"
