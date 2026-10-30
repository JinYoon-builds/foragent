"""D98: bounded checks of the planning skill's public instructions."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def section(number):
    source = (ROOT / "skills/fa-order/SKILL.md").read_text()
    match = re.search(rf"^## {number}\. [^\n]*\n(.*?)(?=^## |\Z)", source, re.M | re.S)
    assert match, f"Keep step {number} in the skill"
    return " ".join(match[1].casefold().split())


def test_step_four_limits_plans_to_what_and_leaves_how_to_the_lane():
    text = section(4)
    assert re.search(r"\b(?:plans?|issues?)\b.{0,100}\b(?:name|describe|specify|state)\b"
                     r".{0,100}\bgoal\b.{0,100}\bscope\b", text)
    assert re.search(r"observable behavio[u]?r.{0,100}(?:inter-issue|between issues|shared) contracts", text)
    assert re.search(r"(?:how.{0,100}(?:lane|implementer)|(?:lane|implementer).{0,100}how)", text)
    assert re.search(r"(?:internal|implementation).{0,100}(?:names|structure|shapes|values)"
                     r".{0,120}(?:lane|implementer)|(?:lane|implementer).{0,120}"
                     r"(?:internal|implementation).{0,100}(?:names|structure|shapes|values)", text)


def test_step_five_locks_observable_behaviour_not_internal_helpers():
    text = section(5)
    assert re.search(r"observable behavio[u]?r.{0,100}(?:public|real) entry points", text)
    assert re.search(r"(?:do not|never|must not|cannot).{0,60}import.{0,60}internal (?:helpers|functions)"
                     r".{0,120}unless.{0,160}(?:issue|contract)", text)
    assert re.search(r"(?:issue.{0,100}(?:names|declares).{0,100}(?:function|contract)|"
                     r"function.{0,100}(?:named|declared).{0,100}contract)", text)


def test_step_six_resolves_what_questions_and_shows_lane_choices_once():
    text = section(6)
    assert re.search(r"(?:question|unknown).{0,140}(?:what|observable|contract)", text)
    assert re.search(r"(?:person|human).{0,100}only.{0,100}what|only.{0,100}"
                     r"(?:what.{0,100}(?:person|human)|(?:person|human).{0,100}what)", text)
    assert re.search(r"decide.{0,120}(?:once.{0,100}restatement|restatement.{0,100}once)", text)
    assert "the lane will decide these" in text
    assert not re.search(r"never answer an unknown yourself|until every issue has no unknowns", text)
    assert not re.search(r"settle reversible details yourself", text)
