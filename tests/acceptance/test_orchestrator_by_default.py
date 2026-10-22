"""Acceptance test for orchestrator-by-default. Locked by Foragent; do not edit."""

from pathlib import Path
import re


def orchestrator_by_default_text(skill):
    path = Path(__file__).resolve().parents[2] / "skills" / skill / "SKILL.md"
    return path.read_text(encoding="utf-8")


def orchestrator_by_default_assert_takeover(text):
    normalized = " ".join(text.casefold().split())
    assert re.search(r"(?:not alive|alive.{0,20}false|꺼져|살아.{0,20}않|없으면)", normalized), (
        "the skill must branch on evidence that the orchestrator is not alive")
    assert re.search(r"(?:this|current) session|지금.{0,10}세션|이 세션|현재.{0,10}세션", normalized)
    assert re.search(
        r"fa-orchestrator|orchestrator role|(?:act|take over|serve).{0,40}orchestrator|오케스트레이터.{0,20}(?:역할|맡)",
        normalized,
    ), "this session must take the orchestrator role"


def test_orchestrator_by_default_order_checks_after_publish_and_starts_only_when_absent():
    text = orchestrator_by_default_text("fa-order")
    publish = text.split("## 7. Publish", 1)[1]
    assert "fa orch alive --json" in publish, "publication must be followed by a liveness check"
    orchestrator_by_default_assert_takeover(publish)
    assert "fa-orchestrator" in publish
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
    orchestrator_by_default_assert_takeover(text)


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
