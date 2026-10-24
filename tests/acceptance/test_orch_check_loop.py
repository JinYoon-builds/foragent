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


def test_orch_check_loop_worked_example_distinguishes_flow_from_structure_without_a_fixed_threshold():
    body = text()
    assert "2026-10-24" in body, "Include the measured D82 example from 2026-10-24"
    assert near(body, r"(?:six|6) runs?", r"test[ -]?slots?", 350), "Six runs queued for test slots"
    assert near(body, r"test[ -]?slots?", r"(?:one|1|an?|about an?) hour|60 min", 350), (
        "The example's slot wait was about an hour")
    assert near(body, r"test[ -]?slots?", r"flow", 350), "Slot allocation is a flow problem"
    assert re.search(r"(?:2|two).{0,60}(?:→|->|to|into|increase|raise).{0,60}(?:3|three)"
                     r"|(?:increase|raise).{0,60}(?:2|two).{0,60}(?:3|three)", body, re.I), (
        "The orchestrator fixes the flow by increasing test slots from two to three")
    assert near(body, r"orchestrator", r"(?:slots?|flow)", 400)
    assert near(body, r"test_runner\.py", r"65\s*(?:%|percent)", 200), "Measure the file's 65 % share"
    assert near(body, r"test_runner\.py", r"structur", 350), "Concentrated test time is a structure problem"
    assert near(body, r"test_runner\.py", r"fa orch architect --why", 500), (
        "Call the architect with the measured test time as evidence")
    assert near(body, r"fa orch architect --why", r"65\s*(?:%|percent)", 350), (
        "Pass the actual number in the architect call's evidence")
    assert re.search(r"(?:do not|don't|never|not|without).{0,100}(?:fixed|hard.coded|numeric|automatic).{0,60}threshold"
                     r"|threshold.{0,100}(?:must not|do not|never).{0,80}(?:call|trigger|architect)", body, re.I), (
        "A fixed threshold must not decide when to call the architect")
