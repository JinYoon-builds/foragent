"""Acceptance test for scorecard-skill. Locked by Foragent; do not edit."""

from pathlib import Path
import re

from test_local_truth import record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


def test_scorecard_skill_teaches_cycle_checks_environment_repairs_and_human_tradeoffs():
    path = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    text = " ".join(path.read_text().casefold().split())
    assert "fa orch scorecard" in text, "The orchestrator skill must read fa orch scorecard"
    cycle = r"(?:each|every).{0,35}(?:cycle|turn|wake|iteration)"
    assert re.search(rf"(?:{cycle}).{{0,250}}fa orch scorecard|fa orch scorecard.{{0,250}}(?:{cycle})", text)
    assert re.search(r"(?:repeat|same).{0,180}(?:two|twice|2).{0,180}environment|"
                     r"(?:two|twice|2).{0,180}(?:repeat|same).{0,180}environment", text)
    assert "root cause" in text and "log" in text
    assert re.search(r"(?:file|open|create|raise).{0,100}(?:fix|repair).{0,100}issue|"
                     r"(?:file|open|create|raise).{0,100}issue.{0,100}(?:fix|repair)", text)
    # An ordinary merge approval paragraph is insufficient: this instruction
    # must concern weakening the checks and cover all three quality safeguards.
    passages = [text[max(0, match.start() - 250):match.end() + 400]
                for match in re.finditer(r"weaken|reduc|relax|loos|lower", text)]
    safeguards = [part for part in passages
                  if re.search(r"ask|confirm|approval|permission", part)
                  and re.search(r"human|user|person|jin", part)]
    assert any(all(word in part for word in ("locked", "review", "merge")) for part in safeguards), (
        "Ask the person before weakening locked tests, cross review, or merge conditions")


def test_scorecard_skill_reviewer_checks_for_tests_that_assert_nothing(
    git_history, tmp_path, monkeypatch,
):
    from foragent.runner import run_issue

    record_gh(monkeypatch)
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true")
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.reviewer.calls) == 1
    prompt = case.reviewer.calls[0]["prompt"]
    assert re.search(r"(?:assert|check)s?\s+nothing|empty\s+tests?", prompt, re.I), (
        "The runner's actual reviewer prompt must flag tests that assert/check nothing")
