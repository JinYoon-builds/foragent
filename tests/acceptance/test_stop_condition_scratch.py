"""Locked acceptance for stop-condition-scratch.

The product is the executor's goal text, the stop condition a model judges at
every stop. It is read through the same public goal builder that
tests/acceptance/test_stop_condition_provable.py locks; the runner supplies its
return value as the executor's goal. No executor, stop hook or model judgment is
simulated here: these checks read the instructions that judge receives.

Measured defect (2026-11-03): 35 lane runs in two days spent the whole stop-hook
block cap because a manual CLI trial in `mktemp -d` (HOME=$D/home, then
`rm -rf $D`) was judged an edit outside the repository that "deleting it
afterwards does not undo", so the condition could never become true again.
"""

import re

import pytest

from foragent.run_prompts import goal_condition

DIRECTORIES = pytest.mark.parametrize("acceptance_dir", [
    "tests/acceptance/",
    "quality/인수 checks/",
    "quality/" + "인수-checks/" * 600,
], ids=["default-directory", "custom-directory", "oversized-directory"])


def sentences(condition):
    # Split on sentence ends followed by space, so "~/.claude" stays in one sentence.
    return [part for part in re.split(r"(?<=[.!?])\s+", condition) if part]


def matching(condition, *patterns):
    return [s for s in sentences(condition) if all(re.search(p, s, re.I) for p in patterns)]


TEMPORARY = r"\b(?:temp(?:orary)?|tmp|scratch|mktemp)\b"
CREATED_BY_RUN = (r"\b(?:you|this run|the run|your run)\b[^.;]*\b(?:create[sd]?|made|make)\b"
                  r"|\b(?:created|made) by (?:you|this run|the run)\b")
NOT_AN_EDIT = (r"\b(?:do(?:es)? not count|(?:is|are) not (?:an? |your )?(?:outside )?(?:edits?|changes?|violations?)"
               r"|(?:is|are) (?:allowed|permitted|exempt|excluded))\b")
FORBIDDEN = (r"\b(?:must (?:stay|remain|be left) unchanged|(?:stays?|remains?) unchanged"
             r"|must not (?:be )?(?:edit|chang|modif|touch|delet)\w*"
             r"|(?:do|does) not (?:edit|change|modify|touch|delete)"
             r"|still counts?|(?:is|are) (?:still )?(?:forbidden|not allowed))\b")
REAL_EXAMPLES = (r"~/\.claude\b", r"\bother repositor(?:y|ies)\b", r"\blaunchd\b")
END_STATE = (r"\b(?:judg|evaluat|check|assess|decid)\w*\b[^.;]*"
             r"(?:\b(?:final|end|ending|current) state\b|\bstate at the end\b|\bas (?:it|they) (?:is|are) at the end\b)")
EARLIER_IGNORED = (r"\b(?:earlier|past|previous|prior)\b[^.;]*"
                   r"\b(?:do(?:es)? not count|(?:is|are) ignored|no longer counts?|(?:do|does) not matter)\b"
                   r"|\bnot (?:on|by|from) (?:an? |the )?(?:earlier|past|previous|prior)\b")


@DIRECTORIES
def test_scratch_work_in_a_temporary_directory_the_run_created_is_not_an_outside_edit(acceptance_dir):
    # Dropping the scratch exemption, or exempting temporary directories without
    # tying them to ones the run itself created, must fail.
    condition = goal_condition(acceptance_dir)
    assert matching(condition, TEMPORARY, r"\bdirector(?:y|ies)\b", CREATED_BY_RUN, NOT_AN_EDIT), (
        "the stop condition must say that scratch work in a temporary directory the run created "
        f"is not an edit outside the repository:\n{condition[-1500:]}")


@DIRECTORIES
def test_real_files_outside_the_repository_are_still_forbidden(acceptance_dir):
    # A blanket exemption ("files outside the repository do not count") or an
    # exemption with no named real files left forbidden must fail.
    condition = goal_condition(acceptance_dir)
    assert re.search(
        r"\byou\b[^.;]*\b(?:did not|have not)\b[^.;]*\b(?:edit(?:ed)?|modif(?:y|ied)|chang(?:e|ed))\b"
        r"[^.;]*\bfiles outside (?:this|the) repository\b", condition, re.I), condition[-1500:]
    named = [s for s in matching(condition, FORBIDDEN)
             if sum(bool(re.search(example, s)) for example in REAL_EXAMPLES) >= 2]
    assert named, (
        "the stop condition must still forbid changing the person's real files outside the repository, "
        f"naming at least two of ~/.claude, other repositories and launchd:\n{condition[-1500:]}")
    assert not any(re.search(example, s) for s in matching(condition, NOT_AN_EDIT)
                   for example in REAL_EXAMPLES if not re.search(FORBIDDEN, s, re.I)), (
        f"real outside files must not be listed as exempt:\n{condition[-1500:]}")


@DIRECTORIES
def test_stop_condition_is_judged_on_the_end_state_not_on_earlier_steps(acceptance_dir):
    # A condition that lets an earlier transcript step (a failed command, a
    # cleaned-up write) keep it false forever must fail.
    condition = goal_condition(acceptance_dir)
    assert matching(condition, END_STATE), (
        f"the stop condition must be judged on the state at the end of the run:\n{condition[-1500:]}")
    assert matching(condition, EARLIER_IGNORED), (
        f"earlier steps that the end state no longer shows must not keep the condition false:\n{condition[-1500:]}")
