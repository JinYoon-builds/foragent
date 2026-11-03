"""Locked acceptance checks for request-answer-record; authored before implementation.

Public CLI assumptions: request-status accepts --answer; role-decision accepts
--answer alongside its existing accepted/rejected argument and --role. Inbox JSON
exposes the supplied answer and, for a proposal, role and boolean accepted.
These are observable contracts, not requirements on storage or helper functions.

Break-it: losing the answer, losing/reversing a decision, counting a rejection,
counting a retry twice, or completing silently must fail these checks. No test
seeds answers, proposal decisions, or scorecard totals.
"""

import json

import pytest

from test_order_progress import offline_local_cli
from test_role_source import DEFAULTS, role_entry, write_role_snapshot
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


@pytest.fixture
def request_case(tmp_path, monkeypatch, isolated_home):  # noqa: F811
    import socket
    import subprocess

    case = offline_local_cli(tmp_path, monkeypatch)
    write_role_snapshot(case.root, {name: role_entry(name) for name in DEFAULTS})

    def unexpected(*args, **kwargs):
        pytest.fail("Request completion and scorecard reads must stay local")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)
    return case


def command(case, capsys, *args):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", *args, "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert case.gh_calls == [], "These commands must not call GitHub"
    return code, output


def rows(case, capsys):
    code, output = command(case, capsys, "inbox", "--json")
    assert code == 0, output.err
    return {row["id"]: row for row in json.loads(output.out)}


def request(case, capsys, text, source="planning"):
    code, output = command(case, capsys, "request", "--from", source, "--text", text)
    assert code == 0, output.err
    [row] = [row for row in rows(case, capsys).values() if row["text"] == text]
    return row["id"]


def accepted_count(case, capsys):
    code, output = command(case, capsys, "scorecard", "--json")
    assert code == 0, output.err
    return json.loads(output.out)["window"]["today"]["roles"]["architect"]["accepted_proposals"]


def test_completion_persists_the_answer_without_counting_an_ordinary_request(request_case, capsys):
    case = request_case
    request_id = request(case, capsys, "Prepare the requested order")
    answer = "Prepared two independent issues and their locked acceptance checks."
    code, output = command(case, capsys, "request-status", request_id, "done", "--answer", answer)
    assert code == 0, f"One closing command must accept the answer: {output.err}"
    # Every CLI invocation opens state again; stdout from completion is not evidence.
    closed = rows(case, capsys)[request_id]
    assert closed["status"] == "done"
    assert closed.get("answer") == answer, "Inbox JSON must retain what was done"
    assert accepted_count(case, capsys) == 0, "Ordinary completions are not accepted proposals"


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_one_proposal_closing_command_records_answer_decision_and_scorecard_once(
        request_case, capsys, decision):
    case = request_case
    request_id = request(case, capsys, "Consider splitting the shared module", source="architect")
    untouched_id = request(case, capsys, "Leave this unrelated request open")
    untouched = rows(case, capsys)[untouched_id]
    answer = ("Applied the proposed split in the published order." if decision == "accepted"
              else "Kept the module together because its callers need one transaction.")
    closing = ("role-decision", request_id, decision, "--role", "architect", "--answer", answer)
    code, output = command(case, capsys, *closing)
    assert code == 0, f"One closing command must take both answer and decision: {output.err}"
    current = rows(case, capsys)
    assert current[request_id]["status"] == "done"
    assert current[request_id].get("answer") == answer, "Closing must not drop the answer"
    assert current[request_id].get("role") == "architect"
    assert current[request_id].get("accepted") is (decision == "accepted"), (
        "Inbox JSON must expose the explicit proposal decision, not infer it from done"
    )
    assert current[untouched_id] == untouched, "Closing one request must leave its neighbor alone"
    expected = int(decision == "accepted")
    assert accepted_count(case, capsys) == expected, "Count accepted proposals, excluding rejections"
    # A retry may be refused or succeed idempotently, but must not count twice.
    command(case, capsys, *closing)
    retried = rows(case, capsys)
    assert retried[request_id].get("answer") == answer
    assert retried[request_id].get("accepted") is (decision == "accepted")
    assert retried[request_id]["status"] == "done"
    assert retried[untouched_id] == untouched
    assert accepted_count(case, capsys) == expected, "Retrying a close must not duplicate an acceptance"
    assert accepted_count(case, capsys) == expected, "Reading the scorecard must not change its count"


@pytest.mark.parametrize("closing", [
    ("request-status", "done"),
    ("role-decision", "accepted", "--role", "architect"),
    ("role-decision", "rejected", "--role", "architect"),
], ids=["ordinary", "accepted-proposal", "rejected-proposal"])
def test_a_bare_close_is_refused_or_records_an_explicit_explanation(request_case, capsys, closing):
    case = request_case
    request_id = request(case, capsys, "A request with no supplied completion answer", source="architect")
    before = rows(case, capsys)[request_id]
    code, _output = command(case, capsys, closing[0], request_id, *closing[1:])
    after = rows(case, capsys)[request_id]
    if code != 0:
        assert after == before, "Refusing a missing answer must leave the request untouched"
        assert accepted_count(case, capsys) == 0, "A refused close must not create an acceptance"
    else:
        assert after["status"] == "done"
        explanation = after.get("answer") or after.get("empty_answer_reason")
        assert isinstance(explanation, str) and explanation.strip(), (
            "A successful close must never silently leave no answer; record an explicit empty reason"
        )
        if closing[0] == "role-decision":
            assert after.get("accepted") is (closing[1] == "accepted")
            assert accepted_count(case, capsys) == int(closing[1] == "accepted")
