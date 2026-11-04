"""Acceptance: new orchestrator requests reach the planning session's next prompt.

The orchestrator hands work to the planning (PM) session with
`fa orch request --from orchestrator`. The planning session's UserPromptSubmit
hook (`fa statusline --prompt-context`) is the only path for that hand-off, so a
request still marked new must be in the very next turn's box facts, not only
once it has been stuck for an hour. Observed only through the installed prompt
hook and the `fa orch` CLI.
"""

from datetime import timedelta

import pytest

from foragent import cli, orch_inbox
from test_fa_report_since import report_case  # noqa: F401
from test_presence_order_stall import prompt
from test_presence_session_start import session_case  # noqa: F401
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


@pytest.fixture(autouse=True)
def checkout_origin(report_case):
    # The same local origin identity the prompt-hook tests use: no Git processes, no remote.
    (report_case.root / ".git/config").write_text(
        '[remote "origin"]\n\turl = git@github.com:offline/reports.git\n')


def orchestrator_request(case, text):
    """Send a request the way the orchestrator does, and return its record."""
    before = {row["id"] for row in orch_inbox.requests(case.store)}
    assert cli_exit(cli.main, ["orch", "request", "--from", "orchestrator", "--text", text,
                               "--repo-root", str(case.root)]) == 0
    (row,) = [row for row in orch_inbox.requests(case.store) if row["id"] not in before]
    assert row["status"] == "new"
    return row


def request_status(case, request_id, status):
    """Move a request on with the public CLI; a closing that one command refuses takes the other."""
    root = ["--repo-root", str(case.root)]
    if status != "done":
        assert cli_exit(cli.main, ["orch", "request-status", request_id, status, *root]) == 0
        return
    answer = ["--answer", "Handled in the planning session"]
    if cli_exit(cli.main, ["orch", "request-status", request_id, "done", *answer, *root]) != 0:
        # An orchestrator request may count as a role proposal, closed with role-decision.
        assert cli_exit(cli.main, ["orch", "role-decision", request_id, "accepted",
                                   "--role", "orchestrator", *answer, *root]) == 0
    assert next(row for row in orch_inbox.requests(case.store) if row["id"] == request_id)["status"] == "done"


def test_every_new_orchestrator_request_is_in_the_next_prompt_once(session_case, monkeypatch):
    case = session_case
    now = case.clock[0]
    case.clock[0] = now - timedelta(minutes=10)
    older = orchestrator_request(case, "Opaque order r396: Ξ71")
    case.clock[0] = now
    fresh = orchestrator_request(case, "Opaque order r404: Ϙ58")
    for _ in range(2):  # still unhandled, so still shown on the following turn
        context = prompt(case, monkeypatch)
        for row in (older, fresh):
            assert row["id"] in context, f"Planning agent lacks new request {row['id']}"
            assert row["text"] in context, f"Planning agent lacks the text of {row['id']}"
            assert context.count(row["text"]) == 1, f"New request {row['id']} must be listed once"


def test_processing_or_done_requests_are_not_shown_as_new(session_case, monkeypatch):
    case = session_case
    taken = orchestrator_request(case, "Opaque taken request Ж12")
    closed = orchestrator_request(case, "Opaque closed request Ю34")
    waiting = orchestrator_request(case, "Opaque waiting request Ψ56")
    request_status(case, taken["id"], "processing")
    request_status(case, closed["id"], "processing")
    request_status(case, closed["id"], "done")
    context = prompt(case, monkeypatch)
    assert waiting["text"] in context, "The request still marked new must stay visible"
    # A request the planning session already took (under the 60-minute stall line) or
    # finished is no longer news; it must not be offered again as a new request.
    assert taken["text"] not in context
    assert closed["text"] not in context


def test_stuck_requests_still_shown_next_to_new_ones(session_case, monkeypatch):
    case = session_case
    now = case.clock[0]
    case.clock[0] = now - timedelta(hours=3)
    stuck = orch_inbox.add_request(case.store, "architect", "Opaque stuck request Ω89")
    orch_inbox.set_request_status(case.store, stuck["id"], "processing")
    case.clock[0] = now
    fresh = orchestrator_request(case, "Opaque new request Λ27")
    context = prompt(case, monkeypatch)
    for value in (stuck["id"], stuck["text"], "3", fresh["id"], fresh["text"]):
        assert value in context, f"Planning agent lacks box fact {value!r}"


@pytest.mark.parametrize("role", ["orchestrator", "lane"])
def test_role_sessions_do_not_receive_new_requests(session_case, monkeypatch, role):
    case = session_case
    fresh = orchestrator_request(case, "Opaque request for planning only Φ40")
    monkeypatch.setenv("FORAGENT_ROLE", role)
    assert fresh["text"] not in prompt(case, monkeypatch)
