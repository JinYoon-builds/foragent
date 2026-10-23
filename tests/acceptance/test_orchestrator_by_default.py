"""Acceptance test for orchestrator-by-default. Locked by Foragent; do not edit."""

from pathlib import Path
import re

from test_skills_autosync import isolated_home
from test_squirrel_running import view_offline


def orchestrator_by_default_text(skill):
    path = Path(__file__).resolve().parents[2] / "skills" / skill / "SKILL.md"
    return path.read_text(encoding="utf-8")


def orchestrator_by_default_assert_planning(text):
    normalized = " ".join(text.casefold().split())
    for command in ("fa orch alive", "fa orch ensure", "fa orch request", "fa gate watch --open",
                    "fa plan heartbeat", "fa orch questions"):
        assert command in normalized, f"the planning session needs {command}"
    assert re.search(r"(?:not alive|alive.{0,20}false|꺼져|살아.{0,20}않|없으면)", normalized), (
        "the skill must branch on evidence that the orchestrator is not alive")
    assert re.search(
        r"(?:never|must not|do not|does not).{0,100}(?:become|take over|assume|act as).{0,80}orchestrator"
        r"|(?:기획|이|현재).{0,20}세션.{0,80}오케스트레이터.{0,40}(?:않|아니)",
        normalized,
    ), "D76 keeps the planning session out of the orchestrator role"
    assert re.search(r"(?:not alive|absent|alive.{0,20}false|없으면).{0,240}fa orch ensure", normalized)
    assert re.search(r"background|백그라운드", normalized)
    assert re.search(r"cross.session|session.to.session|세션 간", normalized)
    assert "--from planning" in normalized
    assert re.search(r"(?:every|each|all).{0,100}request.{0,180}(?:message|fa orch request)"
                     r"|request.{0,100}(?:both|also).{0,180}(?:message|fa orch request)", normalized)
    assert re.search(r"(?:beside|next to|alongside).{0,80}(?:planning|this|current).{0,30}session"
                     r"|기획 세션.{0,20}옆", normalized)
    assert re.search(r"(?:while|as long as).{0,80}(?:open|active).{0,120}(?:heartbeat|renew)"
                     r"|(?:heartbeat|renew).{0,120}(?:while|as long as).{0,80}(?:open|active)", normalized)
    assert re.search(r"(?:show|present|surface|display).{0,100}(?:open|pending).{0,40}questions"
                     r"|(?:열린|답할).{0,30}질문.{0,40}(?:보여|표시)", normalized)


def test_orchestrator_by_default_order_checks_after_publish_and_starts_only_when_absent():
    text = orchestrator_by_default_text("fa-order")
    publish = text.split("## 7. Publish", 1)[1]
    assert "fa orch alive --json" in publish, "publication must be followed by a liveness check"
    assert "fa orch ensure" in publish, "publication must ensure a background orchestrator when absent"
    orchestrator_by_default_assert_planning(text)
    normalized = " ".join(publish.casefold().split())
    assert re.search(
        r"(?:already alive|is alive|alive.{0,20}true|살아 있).{0,240}"
        r"(?:do not|don't|never|no second|start nothing|새로.{0,20}않|중복.{0,20}않)", normalized,
    ), "an existing live orchestrator must suppress a second session"


def test_orchestrator_by_default_fa_session_takes_over_running_work_when_watcher_is_absent():
    from test_fa_entry_skill import entry_skill_text

    text = entry_skill_text()
    assert "fa orch alive" in text
    normalized = " ".join(text.casefold().split())
    assert re.search(r"running work|work is running|approved work|일이.{0,20}돌|실행 중|작업.{0,20}진행", normalized)
    # Historical baseline name retained; D76 replaces takeover with delegation.
    orchestrator_by_default_assert_planning(text)


def test_orchestrator_by_default_watching_claim_requires_a_running_heartbeat_writer():
    text = orchestrator_by_default_text("fa-orchestrator")
    assert "fa orch watch" in text
    paragraphs = [" ".join(part.casefold().split()) for part in re.split(r"\n\s*\n", text)]
    guards = [part for part in paragraphs
              if re.search(r"watching|지켜보는 중", part)
              and re.search(r"only|unless|never|때만|않|경우에만", part)
              and re.search(r"fa orch watch|watch(?:er)?", part)
              and re.search(r"running|active|실행 중|돌고|동작 중", part)]
    assert guards, "explicitly restrict 'watching' claims to a running fa orch watch heartbeat writer"


def orchestrator_by_default_patch_spawn(monkeypatch):
    from foragent import cli, orch, orch_spawn, scheduler

    original = orch_spawn.ensure_orchestrator
    calls = []

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return "background"

    for module in (orch_spawn, scheduler, cli, orch):
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, spawn)
    return calls


def test_orchestrator_by_default_ensure_calls_the_same_spawn_function_as_the_gate_once(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, tick_context
    from test_orch_spawn import orch_spawn_case
    from test_skills_autosync import cli_exit

    case, commands, pushes = orch_spawn_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    calls = orchestrator_by_default_patch_spawn(monkeypatch)
    tick(tick_context(case, start_runs=False))
    assert len(calls) == 1, "the gate must use the shared orchestrator spawn function"
    calls.clear()
    assert cli_exit(main, ["orch", "ensure", "--repo-root", str(case.root)]) == 0
    assert len(calls) == 1, "fa orch ensure must delegate to that same spawn function once"
    args, kwargs = calls[0]
    values = [*args, *kwargs.values()]
    assert all(value in values for value in (case.root, case.store, case.config))
    assert commands == pushes == []


def test_orchestrator_by_default_ensure_does_not_call_spawn_for_a_live_orchestrator(
    git_history, tmp_path, monkeypatch,
):
    from test_heartbeats import heartbeats_write
    from test_orch_review_handoff import bind_cli
    from test_orch_spawn import orch_spawn_case
    from test_skills_autosync import cli_exit

    case, commands, pushes = orch_spawn_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    calls = orchestrator_by_default_patch_spawn(monkeypatch)
    heartbeats_write(case.store, "orch", case.clock[0])
    assert cli_exit(main, ["orch", "ensure", "--repo-root", str(case.root)]) == 0
    assert calls == commands == pushes == []
