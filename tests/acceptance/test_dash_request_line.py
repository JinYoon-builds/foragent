"""Acceptance tests for dash-request-line: the dashboard hero box shows only planning
requests meant for the person; a routing header gives way to the request's next line.

A plain first line already shows today and stays guarded by existing tests, so it is not
repeated here.

They drive the public path the person sees: `fa orch request` / `fa orch request-status`
write the durable box, a gate tick publishes the dashboard snapshot, and
`fa gate watch --once` / `fa dashboard --once` print the hero box.
"""

import json

import pytest

from test_squirrel_running import plain_frame

LABEL = {"ko": "기획 세션 요청:", "en": "Planning request:"}
HEADER = "Decision: orchestrator"


def dashboard_case(git_history, tmp_path, monkeypatch, language):
    from test_view_status import status_case

    return status_case(git_history, tmp_path, monkeypatch, language=language)


def root_args(case):
    return ["--repo-root", str(case.root)]


def draw(case, main, capsys, command):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    tick(tick_context(case, start_runs=False))
    capsys.readouterr()
    assert main([*command, "--once", *root_args(case)]) == 0
    return plain_frame(capsys.readouterr().out)


def send(case, main, capsys, source, text, status="processing"):
    """Put one request in the box as `source` and set its status through the CLI."""
    assert main(["orch", "request", "--from", source, "--text", text, *root_args(case)]) == 0
    capsys.readouterr()
    assert main(["orch", "inbox", "--json", *root_args(case)]) == 0
    rows = [row for row in json.loads(capsys.readouterr().out) if row["text"] == text]
    assert rows, f"the box must hold the request from {source}"
    request = rows[-1]
    if status != "new":
        assert main(["orch", "request-status", str(request["id"]), status, *root_args(case)]) == 0
        capsys.readouterr()
    return request


def request_lines(frame, language):
    return [line for line in frame.splitlines() if LABEL[language] in line]


COMMANDS = [("gate", "watch"), ("dashboard",)]


@pytest.mark.parametrize("command", COMMANDS, ids=["gate-watch", "dashboard"])
@pytest.mark.parametrize("language", ["ko", "en"])
def test_architect_routing_proposal_never_reaches_the_planning_request_line(
    git_history, tmp_path, monkeypatch, capsys, language, command,
):
    case, main = dashboard_case(git_history, tmp_path, monkeypatch, language)
    send(case, main, capsys, "architect",
         f"{HEADER}\nSplit watch_layout into smaller modules\nWhy: it grew past 400 lines.")
    frame = draw(case, main, capsys, list(command))
    assert request_lines(frame, language) == [], (
        "an architect proposal for the orchestrator is not a planning request; "
        f"the hero box must show no planning-request line:\n{frame}"
    )
    assert HEADER not in frame, f"the routing header must stay off the hero box:\n{frame}"
    assert "Split watch_layout into smaller modules" not in frame, (
        f"the architect's proposal subject must not appear as a planning request:\n{frame}"
    )


@pytest.mark.parametrize("command", COMMANDS, ids=["gate-watch", "dashboard"])
@pytest.mark.parametrize("language", ["ko", "en"])
def test_planning_request_line_shows_the_first_content_line_after_a_routing_header(
    git_history, tmp_path, monkeypatch, capsys, language, command,
):
    case, main = dashboard_case(git_history, tmp_path, monkeypatch, language)
    send(case, main, capsys, "planning", f"{HEADER}\nPlan the login order\nDetails follow.")
    frame = draw(case, main, capsys, list(command))
    lines = request_lines(frame, language)
    assert len(lines) == 1, f"one planning-request line is expected, once:\n{frame}"
    assert f"{LABEL[language]} Plan the login order" in lines[0], (
        f"the line must show the request's first content line, not its routing header:\n{frame}"
    )
    assert HEADER not in frame, f"the routing header must stay off the hero box:\n{frame}"
    assert "Details follow." not in frame, "only the first content line is shown"


@pytest.mark.parametrize("language", ["ko", "en"])
def test_newer_architect_proposal_does_not_replace_the_planning_request(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = dashboard_case(git_history, tmp_path, monkeypatch, language)
    send(case, main, capsys, "planning", "Plan the login order")
    send(case, main, capsys, "architect", f"{HEADER}\nSplit watch_layout into smaller modules")
    frame = draw(case, main, capsys, ["gate", "watch"])
    lines = request_lines(frame, language)
    assert len(lines) == 1, f"one planning-request line is expected:\n{frame}"
    assert f"{LABEL[language]} Plan the login order" in lines[0], (
        f"the line must keep the planning session's request, not the newer role proposal:\n{frame}"
    )
    assert HEADER not in frame and "Split watch_layout" not in frame


@pytest.mark.parametrize("language", ["ko", "en"])
def test_planning_request_line_disappears_when_only_role_proposals_remain(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = dashboard_case(git_history, tmp_path, monkeypatch, language)
    planning = send(case, main, capsys, "planning", "Plan the login order")
    send(case, main, capsys, "architect", f"{HEADER}\nSplit watch_layout into smaller modules")
    assert len(request_lines(draw(case, main, capsys, ["gate", "watch"]), language)) == 1
    assert main(["orch", "request-status", str(planning["id"]), "done",
                 "--answer", "Order published", *root_args(case)]) == 0
    capsys.readouterr()
    frame = draw(case, main, capsys, ["gate", "watch"])
    assert request_lines(frame, language) == [], (
        f"with no open planning request, the planning-request line is not shown:\n{frame}"
    )
    assert HEADER not in frame
