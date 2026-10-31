"""Locked acceptance for stop-condition-provable.

The product is the executor's goal text. Reuse the public goal builder exercised
by tests/acceptance/test_session_resume_prompt.py; the runner supplies its return
value as the executor's goal (test_claude_goal_prompt.py checks that delivery).
No executor, stop hook, filesystem scan, or model judgment is simulated here.
"""

import re

import pytest

from foragent.run_prompts import goal_condition


def contract_violations(condition, acceptance_dir, *, oversized=False):
    """Check the observable instructions, including the budget as a sole trigger."""
    problems = []
    if "no files outside this repository are changed" in condition.casefold():
        problems.append("goal demands proof that no files outside this repository are changed")
    if not re.search(
        r"\b(?:you|the implementer)\b[^.;]*\b(?:did not|ha(?:ve|s) not)\b"
        r"[^.;]*\b(?:edit(?:ed)?|modif(?:y|ied)|chang(?:e|ed))\b"
        r"[^.;]*\bfiles outside (?:this|the) repository\b",
        condition, re.I,
    ):
        problems.append("outside-file restriction must cover the implementer's own edits")
    exemption = re.search(
        r"[^.;]*\b(?:test tooling|test tools)\b[^.;]*"
        r"\b(?:do(?:es)? not count|(?:is|are) exempt|(?:is|are) excluded)\b[^.;]*",
        condition, re.I,
    )
    if exemption is None or not re.search(
        r"\boutside (?:this |the )?repo(?:sitory)?\b", exemption[0], re.I,
    ):
        problems.append("test tooling output outside the repository must explicitly not count")
    if not re.search(
        r'Every command in the "Done when" list below exits 0 when run from the repository root',
        condition,
    ):
        problems.append("keep the Done when commands passing from the repository root")
    locked = re.search(r"nothing under (.*?) is modified", condition, re.S)
    if locked is None or not locked[1] or not acceptance_dir.startswith(locked[1]):
        problems.append("keep the unchanged-acceptance-directory text and its supplied path")
    elif not oversized and locked[1] != acceptance_dir:
        problems.append("an ordinary acceptance-directory path must remain complete")
    if len(condition) > 3500 or (oversized and len(condition) != 3500):
        problems.append("keep the 3500-character cap and truncate only the acceptance path")
    if not re.search(
        r"(?:^|[.!?]\s+)(?:or\s+)?(?:at|after) 40 turns,?\s+"
        r"(?:end|stop|finish|terminate) (?:the |this )?(?:run|session),?\s+"
        r"without (?:repeating|rerunning) (?:the )?(?:stop|goal) (?:check|condition)"
        r"(?:[.!?]|$)", condition, re.I,
    ):
        problems.append("40 turns must end the run independently, without repeating the stop check")
    return problems


@pytest.mark.parametrize("acceptance_dir,oversized", [
    ("tests/acceptance/", False),
    ("quality/인수 checks/", False),
    ("quality/" + "인수-checks/" * 600, True),
], ids=["default-directory", "custom-directory", "oversized-directory"])
def test_executor_goal_contains_only_provable_completion_conditions(acceptance_dir, oversized):
    # Returning a wrong global requirement, omitting the tooling exemption or
    # own-edit scope, dropping either completion check, or losing the independent
    # turn-limit exit must fail. The long case also catches suffix truncation.
    condition = goal_condition(acceptance_dir)
    problems = contract_violations(condition, acceptance_dir, oversized=oversized)
    assert not problems, "\n".join(problems)
