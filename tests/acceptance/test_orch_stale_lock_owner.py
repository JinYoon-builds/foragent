"""Acceptance test for orch-stale-lock-owner. Locked by Foragent; do not edit.

A locked acceptance test can go stale without anyone changing its purpose: merged main
adds test cases, changes how many passes something takes, or changes a behaviour the
test only assumed. The orchestrator corrects such stale expectations itself, as the owner
of every locked test, through an order edit and re-approval. Only a question about what
the product should do goes to the PM.
"""

from pathlib import Path
import re


def skill_text():
    path = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    return " ".join(path.read_text(encoding="utf-8").lower().split())


def test_orchestrator_owns_every_locked_test_for_stale_expectations():
    text = skill_text()
    stale = r"(?:stale|outdated|out of date)"
    assert re.search(stale + r".{0,200}(?:locked|acceptance)|(?:locked|acceptance).{0,200}" + stale, text), (
        "The skill must name stale expectations of locked acceptance tests")
    assert re.search(r"(?:every|any|all).{0,40}locked.{0,40}tests?|locked.{0,40}tests?.{0,60}(?:whoever|no matter who|regardless)",
                     text), "Ownership must cover every locked test, not only tests the orchestrator wrote"
    for cause in (r"(?:count|number of).{0,40}(?:case|test|pass)", r"(?:merged|main).{0,80}chang"):
        assert re.search(cause, text), cause


def test_stale_expectation_fix_is_published_by_the_orchestrator_and_only_behaviour_goes_to_the_pm():
    text = skill_text()
    assert re.search(r"(?:stale|outdated|out of date).{0,400}(?:order edit|publish).{0,200}(?:re.?approv|--approve)", text), (
        "A stale expectation is fixed through an order edit the orchestrator publishes and re-approves")
    assert re.search(r"(?:purpose|intent).{0,80}(?:unchanged|same|kept|still holds)", text), (
        "The skill must say the test's purpose stays unchanged in this case")
    assert re.search(r"(?:product|what the product).{0,60}(?:behaviou?r|should do).{0,200}\bpm\b"
                     r"|\bpm\b.{0,200}(?:product|what the product).{0,60}(?:behaviou?r|should do)", text), (
        "Only a product-behaviour question goes to the PM")


def test_regression_only_done_when_lines_are_dropped_because_pr_ci_guards_them():
    text = skill_text()
    assert re.search(r"regression.{0,200}(?:done.when|done_when).{0,300}(?:pass|green).{0,120}main"
                     r"|(?:done.when|done_when).{0,200}regression.{0,300}(?:pass|green).{0,120}main", text), (
        "The skill must name regression-only Done when lines that already pass on main")
    assert "done_when_removals" in text, "They are dropped through done_when_removals with a reason"
    assert re.search(r"(?:pr|pull request) ci.{0,80}(?:guard|protect|catch|cover)", text), (
        "The reason is that PR CI guards regressions")
