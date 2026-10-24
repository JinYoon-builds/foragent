"""Acceptance test for orch-check-loop. Locked by Foragent; do not edit."""

from pathlib import Path
import re

SKILL = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"


def text():
    return " ".join(SKILL.read_text(encoding="utf-8").split())


def near(body, first, second, span=300):
    return re.search(rf"(?:{first}).{{0,{span}}}(?:{second})|(?:{second}).{{0,{span}}}(?:{first})", body, re.I)


def test_orch_check_loop_runs_after_merges_on_a_timer_and_in_the_morning():
    body = text()
    assert near(body, r"after (?:each|every) merge", r"check", 200), "Run a check after every merge"
    assert re.search(r"(?:every|each) 30 min", body, re.I), "Run a check every 30 minutes"
    assert near(body, r"morning", r"(?:plan|brief)", 200), "Write the day's plan and briefing in the morning"
    for item in (r"goal", r"idle lane|lanes? (?:with nothing|that sit idle|without work)",
                 r"overlapping check|checks? (?:that )?overlap", r"quota", r"repeated failure|fail(?:s|ures)? repeat"):
        assert re.search(item, body, re.I), f"The check loop must look at: {item}"


def test_orch_check_loop_steers_inside_the_goal_with_the_plan_tools_and_records_why():
    body = text()
    assert near(body, r"(?:current|active) goal", r"order|resource", 300), (
        "Hold the current goal and decide order and resources inside it")
    for tool in ("fa orch queue", "fa orch pause", "fa orch resume"):
        assert tool in body, f"Use the plan tool {tool}"
    assert re.search(r"--why|record (?:why|the reason)|reason (?:is|gets) recorded", body, re.I), (
        "Record why each change was made")
    assert near(body, r"blocked_by", r"(?:real|true) dependenc", 200), (
        "Keep blocked_by for real dependencies, not for ordering")


def test_orch_check_loop_uses_pstack_procedures_when_failures_repeat():
    body = text()
    assert near(body, r"repeat|twice|again", r"references/(?:bug-fix|investigate)", 400), (
        "When failures repeat, follow the measure-first procedures in references/")
