"""Presentation records reuse the issue-waits owner's canonical section reader."""

import pytest

from foragent import order_waiting


def test_waiting_record_preserves_values_and_the_canonical_section_boundary():
    raw = "Reason: Choice  pending: locale\nNext condition: Jin records a choice\nResponsible role: orchestrator"
    body = "## Waiting\n" + raw + "\n<!-- foragent:child plan=p key=k -->\nReason: obsolete"
    assert order_waiting.read_waiting(body) == {
        "text": raw, "reason": "Choice  pending: locale", "next_condition": "Jin records a choice",
        "responsible_role": "orchestrator",
    }


@pytest.mark.parametrize("body", [None, "", "## Goal\nNo wait", "## Waiting\n", "## Waiting\nNone.\n"])
def test_missing_or_cleared_wait_has_no_record(body):
    assert order_waiting.read_waiting(body) is None


@pytest.mark.parametrize("raw", ["Reason: pending", "Await a choice.", "Reason: A\nReason: B",
                               "Reason: \nNext condition: B\nResponsible role: C",
                               "Reason: A\nNext condition: B\nResponsible role: C\n### Details\nKeep me"])
def test_nonstandard_waits_are_kept_whole_instead_of_discarded(raw):
    assert order_waiting.read_waiting("## Waiting\n" + raw) == {"text": raw}


def test_record_reads_the_public_section_once_and_uses_its_return_value(monkeypatch):
    calls = []

    def canonical(body):
        calls.append(body)
        return "From the canonical reader."

    monkeypatch.setattr(order_waiting, "section", canonical)
    assert order_waiting.read_waiting("Other body") == {"text": "From the canonical reader."}
    assert calls == ["Other body"]


def test_projection_reads_owner_replacements_and_their_notification():
    original = "## Goal\nKeep the goal.\n\n## Waiting\nOld wait.\n\n## Done when\nKeep the checks.\n"
    raw = "Reason: Locale choice\nNext condition: Record the locale\nResponsible role: planning"
    changed = order_waiting.replace_section(original, raw)
    changed = order_waiting.stamp(original, changed)
    assert order_waiting.read_waiting(changed) == {
        "text": raw, "reason": "Locale choice", "next_condition": "Record the locale",
        "responsible_role": "planning",
    }
    line = order_waiting.notification(changed)
    assert line.count("Reason: Locale choice") == 1
    assert "Next condition: Record the locale" in line and "Responsible role: planning" in line
    cleared = order_waiting.replace_section(changed, None)
    assert order_waiting.read_waiting(cleared) is None
    assert cleared.startswith("## Goal\nKeep the goal.\n\n## Done when\nKeep the checks.\n")


@pytest.mark.parametrize("body", ["Text", "Text  ", "Text\t", "Text\n", "## Waiting\nA wait"])
def test_appended_markers_always_take_their_own_line(body):
    stamped = order_waiting.stamp("", body if "Waiting" in body else body + "\n\n## Waiting\nNew")
    written = order_waiting.mark_write(body, body + "\nMore")
    for result, kind in ((stamped, "waiting-prior"), (written, "write")):
        assert any(line.startswith(f"<!-- foragent:{kind} ") and line.endswith(" -->")
                   for line in result.splitlines())
    assert order_waiting.section(stamped) is not None


def test_same_write_keeps_its_id_when_only_the_final_newline_differs():
    first = order_waiting.mark_write("Old", "Text")
    assert order_waiting.mark_write(first, "Text") == first
    assert order_waiting.mark_write(first, "Text\n") == first
    assert order_waiting.mark_write(first, "Other") != first


def test_bodies_without_a_marker_stay_byte_for_byte():
    assert order_waiting.stamp("Text", "Text") == "Text"
    assert order_waiting.mark_write("Text", "Text") == "Text"


WRITE = "<!-- foragent:write id=" + "a" * 32 + " -->"
PRIOR = "<!-- foragent:waiting-prior sha256=" + "b" * 64 + " id=" + "c" * 32 + " -->"


@pytest.mark.parametrize("literal", [WRITE, PRIOR])
@pytest.mark.parametrize("shape", [
    "## Done when\n```sh\ncat <<'EOF'\nvalue{m}\nEOF\n```\n",  # A suffix: the newline must survive.
    "## Done when\n```sh\ncat <<'EOF'\n{m}\nEOF\n```\n",  # A whole line inside a closed fence.
    "## Example\n~~~\n{m}\n~~~\n",
    "## Example\nProse ending in {m}\nEnd.\n",
    "## Example\n{m} and text\n",
])
def test_gate_writes_keep_authored_marker_text(shape, literal):
    body = shape.format(m=literal)
    edited = body + "\nMore.\n"
    written = order_waiting.mark_write(body, order_waiting.stamp(body, order_waiting.replace_section(edited, "Wait")))
    assert written.startswith(body)
    from foragent.issue import parse_body
    assert parse_body(written).done_when_commands == parse_body(body).done_when_commands
    rewritten = order_waiting.mark_write(written, order_waiting.replace_section(written, "Other"))
    assert rewritten.startswith(body)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_gate_replaces_its_own_marker_lines_wherever_they_are(newline):
    body = newline.join(["## Goal", "Ship.", WRITE, PRIOR, "## Contracts", "Stays.", ""])
    written = order_waiting.mark_write(body, order_waiting.stamp(body, body + "More." + newline))
    assert written.count("foragent:write") == 1 and written.count("foragent:waiting-prior") <= 1
    assert WRITE not in written
    assert "## Contracts" in written and "Stays." in written


def test_fenced_or_inline_marker_text_is_not_the_gate_record():
    fenced = f"## Example\n~~~\n{PRIOR}\n{WRITE}\n~~~\nInline {WRITE}\n"
    assert order_waiting.notification(fenced) == ""
    same = order_waiting.mark_write(fenced, fenced)
    assert same == fenced
    first = order_waiting.mark_write(fenced, fenced + "More.\n")
    assert first.endswith(" -->\n") and first.count("foragent:write") == 3
    assert order_waiting.mark_write(first, fenced + "More.\n") == first


@pytest.mark.parametrize("opening,closing", [("```", "```"), ("~~~", "~~~"), ("`````", "`````")])
def test_a_fenced_waiting_example_is_not_the_issue_wait(opening, closing):
    example = f"## Goal\nShip.\n{opening}\n## Waiting\nExample wait\n{closing}\n"
    assert order_waiting.section(example) is None and order_waiting.read_waiting(example) is None
    replaced = order_waiting.replace_section(example, "Real wait")
    assert replaced.startswith(example) and order_waiting.section(replaced) == "Real wait"
    assert order_waiting.replace_section(replaced, None).startswith(example)
    assert "Real wait" not in order_waiting.replace_section(replaced, None)


@pytest.mark.parametrize("fence", ["~~~", "```", "`````"])
def test_gate_keeps_marker_lines_in_a_fence_after_waiting_text_with_a_fence_line(fence):
    tail = f"## Contracts\n{fence}\n{PRIOR}\n{WRITE}\n{fence}\n"
    body = f"## Waiting\n{fence}\ngate\n{tail}"
    assert order_waiting.section(body) == f"{fence}\ngate"
    assert order_waiting.notification(body) == ""
    edited = order_waiting.mark_write(body, order_waiting.stamp(body, order_waiting.replace_section(body, "New")))
    assert tail in edited and order_waiting.section(edited) == "New"
    assert order_waiting.mark_write(edited, order_waiting.replace_section(edited, "Later")).count(tail) == 1
