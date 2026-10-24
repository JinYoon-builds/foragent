import pytest

from foragent.reasons import (
    APPROVAL_MARKER,
    GROUP,
    Reason,
    format_approval_comment,
    format_comment,
    parse_approval_comment,
    parse_reason,
)


def test_groups_cover_exact_contract_codes():
    assert GROUP == {
        Reason.SPEC_QUESTION: "decision", Reason.HASH_MISMATCH: "reapprove",
        Reason.ALWAYS_HUMAN_PATH: "review", Reason.ACCEPTANCE_TOUCHED: "review",
        Reason.RISK_HIGH: "review", Reason.RETRIES_EXCEEDED: "tech-failure",
        Reason.STALLED: "tech-failure", Reason.TIMEOUT: "tech-failure",
        Reason.NO_MACHINE_CHECK: "decision",
        Reason.ORCHESTRATOR_TIMEOUT: "tech-failure",
        Reason.REVIEW_DISPUTE: "tech-failure",
        Reason.ACCEPTANCE_BASELINE_UNAVAILABLE: "tech-failure",
    }
    assert set(GROUP) == set(Reason)
    assert str(Reason.TIMEOUT) == "timeout"


@pytest.mark.parametrize("reason", list(Reason))
@pytest.mark.parametrize("label", ["blocked", "needs-human"])
def test_reason_round_trip(reason, label):
    body = format_comment(reason, "Human input is needed.", "Please choose an option.\nKeep this detail.", label=label)
    assert body.splitlines()[0] == f"<!-- foragent:{label} reason={reason.value} -->"
    assert body.splitlines()[1] == "**Human input is needed.**"
    assert body.endswith("Please choose an option.\nKeep this detail.")
    assert parse_reason(body) == reason
    assert parse_reason(body.replace("\n", "\r\n")) == reason


def test_optional_details_and_one_line_summary():
    body = format_comment(Reason.STALLED, "  No progress\n for 30 minutes.  ")
    assert body == "<!-- foragent:needs-human reason=stalled -->\n**No progress for 30 minutes.**"


@pytest.mark.parametrize("body", [
    "", "text\n<!-- foragent:needs-human reason=stalled -->",
    "\n<!-- foragent:needs-human reason=stalled -->",
    " <!-- foragent:needs-human reason=stalled -->",
    "<!-- foragent:needs-human reason=unknown -->",
    "<!-- foragent:needs-human reason=stalled --> suffix",
    "<!-- foragent:needs-human reason=stalled",
])
def test_invalid_or_non_first_line_reason_marker(body):
    assert parse_reason(body) is None


@pytest.mark.parametrize("blobs", [{}, {
    "tests/acceptance/with spaces.py": "a" * 40,
    "tests/acceptance/test_a.py": "b" * 40,
}])
def test_approval_round_trip(blobs):
    values = {
        "body_sha": "c" * 64, "acceptance_blobs": blobs,
        "approved_by": "human-login", "approved_at": "2026-10-20T09:00:00+00:00",
    }
    body = format_approval_comment(**values)
    assert body.splitlines()[0] == (
        f"<!-- foragent:approved sha256={'c' * 64} by=human-login at=2026-10-20T09:00:00+00:00 -->"
    )
    assert body.startswith(APPROVAL_MARKER)
    assert parse_approval_comment(body) == values
    assert parse_approval_comment(body.replace("\n", "\r\n")) == values
    assert body.splitlines()[2:] == [f"- `{path}`: `{sha}`" for path, sha in sorted(blobs.items())]


@pytest.mark.parametrize("body", [
    "", "<!-- foragent:approved -->", "<!-- foragent:approved sha256=abc by=human -->",
    "<!-- foragent:approved sha256=abc at=now by=human -->",
    "prefix\n<!-- foragent:approved sha256=abc by=human at=now -->",
    "\n<!-- foragent:approved sha256=abc by=human at=now -->",
])
def test_invalid_approval_marker(body):
    assert parse_approval_comment(body) is None
