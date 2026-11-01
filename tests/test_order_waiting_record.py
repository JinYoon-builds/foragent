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
