"""Durable preread/approval work. Public queue extension is an inter-issue contract.

The PM explicitly authorizes 'approve if clean'; FA executes that request without
classifying natural language. Clean and QUESTION responses come from a fake agent.
"""

from datetime import UTC, datetime

from foragent import cli
from foragent.config import load_config
from foragent.scheduler import TickContext, tick
from foragent.state import StateStore
from test_one_entrance_seat import enter, invoke, make_pm_case
from test_skills_autosync import isolated_home  # noqa: F401


def enqueue(case, capsys):
    result = invoke(capsys, "order", "preread", case.path, "--background", "--approve-if-clean", "--json")
    assert result.code == 0, "FA must accept durable preread/approval work: " + result.text


def test_enqueue_returns_before_the_agent_and_keeps_the_plan_after_the_shell_closes(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    enter(monkeypatch, capsys, "queue-pm")
    enqueue(case, capsys)
    assert case.calls == [], "The PM shell must only submit the work"
    case.path.unlink()  # Queue a content snapshot, not a dependency on a session's file.
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    assert any(p.is_file() for p in StateStore(case.store.root).root.rglob("*"))
    result = invoke(capsys, "order", "context", "--json")
    assert result.code == 0 and "Offline work" in result.out, "FA must expose pending work after PM exit"


def test_gate_finishes_preread_approval_once_and_records_evidence_on_the_issue(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    enter(monkeypatch, capsys, "departed-pm")
    enqueue(case, capsys)
    case.path.unlink()
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    launched = []
    monkeypatch.setattr(cli, "spawn_runner", lambda *args: lambda n: launched.append(n) or 90000 + n)
    for _ in range(2):
        ctx = TickContext(repo=case.values["repo"], repo_root=case.root, tracker=case.tracker,
                          store=StateStore(case.store.root), config=load_config(case.root), git=case.git,
                          now=lambda: datetime.now(UTC), launcher=lambda n: launched.append(n) or 90000 + n,
                          runner_process_exists=lambda n: True)
        tick(ctx)
    children = [i for i in case.tracker.list_issues() if i.title == "Keep locale"]
    assert len(children) == 1 and "approved" in children[0].labels, "FA must finish the authorized work after PM exit"
    comments = [c.body for c in case.tracker.list_comments(children[0].number)]
    assert any("preread" in c.lower() and "NONE" in c for c in comments), "Keep the actual agent result on the issue"
    assert len(case.calls) == 1, "A second tick must not repeat a completed preread"
    assert sum(e[0] == "issue" and e[1] == "Keep locale" for e in case.events) == 1


def test_a_question_does_not_turn_into_approval_after_the_pm_closes(
    tmp_path, monkeypatch, capsys, isolated_home,
):
    case = make_pm_case(tmp_path, monkeypatch)
    case.worker.replies = ["QUESTION: Which storage region does Jin want?"]
    enter(monkeypatch, capsys, "question-pm")
    enqueue(case, capsys)
    monkeypatch.delenv("FORAGENT_SESSION_ID")
    tick(TickContext(repo=case.values["repo"], repo_root=case.root, tracker=case.tracker,
                     store=StateStore(case.store.root), config=load_config(case.root), git=case.git,
                     now=lambda: datetime.now(UTC), launcher=lambda n: None))
    assert len(case.calls) == 1
    assert not any("approved" in i.labels for i in case.tracker.list_issues())
    result = invoke(capsys, "orch", "questions", "--json")
    assert "Which storage region" in result.out, "The unresolved WHAT question must wait durably for the next PM"
