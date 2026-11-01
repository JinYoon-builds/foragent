"""Implementation and review instructions built from the approved issue."""

from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path

from foragent import cuts
from foragent.issue import ParsedIssue


REVIEW_VERDICT_CONTRACT = (
    "Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` "
    "followed by every finding, each one line with file:line and the concrete failure."
)

REVIEW_PROMPT = (
    "Review this change for defects that break the stated goal, violate Out of scope, "
    "or would make the Done when commands pass without the goal being met "
    "(weakened checks, special-casing test inputs). Ignore style and nice-to-haves. "
    "Do not edit files. " + REVIEW_VERDICT_CONTRACT
)
REVIEW_FEEDBACK_INSTRUCTIONS = (
    "For each review finding, name the defect class it belongs to, "
    "find every other instance of that class in the change, fix or rebut each instance, "
    "and state the class and the instances found."
)
DEFAULT_RETRY_HINT = (
    "Keep the branch's work; for each recorded finding, name the defect class it belongs to, "
    "find every other instance of that class in the change, fix them all, "
    "and state the class and the instances found."
)
SEARCHED_LIST_INSTRUCTIONS = (
    "Fix the whole defect class, not only the cited line: repair every instance across the changed code. "
    "Then write `.foragent/SEARCHED.md` as a list of what you searched for, where you looked and what you found; "
    "the runner puts that list in the pull request. The tools and the search order are yours to choose."
)

BREAK_IT = ("Make the code return a wrong value, skip an effect, or do the effect twice. "
            "If the test still passes, it proves nothing.")
IMPLEMENTER_TEST_RULE = "When you write or fix a test, check it by the break-it rule: " + BREAK_IT

PANEL_MERGE_PROMPT = (
    "Independent reviewers checked this change, each from its own share of the review perspectives. "
    "Their raw findings follow, unedited; several may describe the same defect in different words. "
    "Group the findings by defect class, not by identical text: findings with one root cause are one class, "
    "even when their wording, lines or files differ. Keep only the classes you judge real and reproducible "
    "against the diff above, and leave out every claim you dismiss. For each kept class give one finding "
    "line `path:line — summary`, then indented lines `class: <name>`, the evidence, and every affected "
    "instance (path:line). Your reply is the final review: give the verdict and the kept classes in the "
    "format above. Reviewer verdicts do not count; only yours does."
)

CONFIRM_PROMPT = (
    "Limit this review to confirming that the first-round defect classes below are fixed. "
    "For every class, verify the fix across all instances throughout the changed code, including "
    "instances beyond the cited lines and files. Use all the first-round evidence and affected "
    "instances as the starting point for your own search. Judge the implementer's rebuttals. "
    "This is one confirmation task, not another perspective review or findings merge. "
    "Do not edit files. Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` followed by "
    "every remaining finding, each one line with file:line and the concrete failure."
)


def review_scope_rules(acceptance_dir: str, language: str = "en") -> str:
    """Scope rules for reviewers; PR bodies pass the repository language for the human-read question."""
    from foragent.i18n import text

    return (
        REVIEW_VERDICT_CONTRACT + "\n"
        "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
        f"Tests under {acceptance_dir} that belong to other issues fail on purpose until those issues land; do not report them.\n"
        "Improvements beyond this issue's Goal are not defects.\n"
        "Those grounds in full: FAIL only for a failing or circumvented locked acceptance test, an unmet Goal "
        "(a change to Out of scope or a weakened check is one), a broken repository principle (as the "
        "repository's own instruction and decision files state it), or a real defect: a bug, a race, "
        "data loss or a security hole.\n"
        "A different valid approach from the one you would choose is not a defect and never a reason to FAIL. "
        "Naming, structure or other implementation taste is not a defect either; write it as a NOTE line.\n"
        f"Tests under {acceptance_dir} that this issue lists are locked acceptance tests. If a finding would "
        "require behaviour a locked acceptance test forbids, or forbid behaviour it requires, it is a spec "
        "conflict, not a defect: report it as one line "
        "`SPEC-CONFLICT: <locked test path:line> <what the test requires>; <what the finding wants>` "
        "instead of a finding.\n"
        "A test that asserts nothing or proves nothing is a weakened check. Judge each new or changed "
        f"test by the break-it rule: {BREAK_IT}\n"
        "Write root-cause and structure findings (why the defect could happen, a better design) as separate "
        "plain lines `NOTE: <finding>`, beside the findings. NOTE lines are recorded for the orchestrator "
        "and are never a reason to FAIL.\n"
        + text("review_class_question", language)
    )


def build_review_prompt(
    parsed: ParsedIssue, diff: str, acceptance_dir: str = "tests/acceptance/", *, rebuttals: str = "",
    full_text_dir: Path | None = None,
    risk_notes: str = "", panel_findings: str = "", lane_decisions: str = "",
    task: str = REVIEW_PROMPT,
) -> str:
    """One reviewer's prompt; `panel_findings` makes it the merge call over a panel's raw findings.

    `lane_decisions` is `lane_decisions_section` output: the lane's recorded choices, as context.
    """
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    return (
        f"{task}\n\n{review_scope_rules(acceptance_dir)}"
        + (f"\n\n{risk_notes}" if risk_notes else "")
        + f"\n\nGoal:\n{parsed.goal}\n\nOut of scope:\n{parsed.out_of_scope}"
        + (f"\n\n{lane_decisions}" if lane_decisions else "")
        + f"\n\nDone when commands:\n{commands}\n\nDiff:\n{cuts.head(diff, 60000, full_text_dir, 'review-diff.patch')}"
        + (f"\n\nImplementer rebuttals (judge them):\n{rebuttals}" if rebuttals else "")
        + (f"\n\n{PANEL_MERGE_PROMPT}\n\n{panel_findings}" if panel_findings else "")
    )


def build_confirm_prompt(
    parsed: ParsedIssue, diff: str, acceptance_dir: str = "tests/acceptance/", *, first_review: str,
    rebuttals: str = "", full_text_dir: Path | None = None, lane_decisions: str = "",
) -> str:
    """The complete first-round result and current change for one confirming reviewer."""
    return build_review_prompt(
        parsed, diff, acceptance_dir, rebuttals=rebuttals, full_text_dir=full_text_dir,
        lane_decisions=lane_decisions, task=CONFIRM_PROMPT,
    ) + f"\n\nFirst-round defect classes and evidence (verbatim):\n{first_review}"


def parent_section(parent_context: str) -> str:
    return f"This issue is part of a larger order. Its shared outcome and constraints:\n{parent_context}"


def lane_plan_section(approach: list[str], decisions: list[str]) -> str:
    """The lane's own read-only preread proposal for the approved content, handed back to it."""
    lines = ["Your lane's earlier plan (your read-only preread before approval; "
             "follow it unless the repository shows a better way):"]
    if approach:
        lines += ["Approach:", *(f"- {line}" for line in approach)]
    if decisions:
        lines += ["Decisions you said you would settle yourself:", *(f"- {line}" for line in decisions)]
    return "\n".join(lines)


def lane_decisions_section(decisions: list[str]) -> str:
    """The lane's recorded DECIDE lines for a reviewer: context, never acceptance criteria."""
    if not decisions:
        return ""
    return "\n".join([
        "Implementation choices the lane recorded before approval (its DECIDE lines). They are context "
        "for this review, not acceptance criteria: settling one differently, or as recorded, is never "
        "by itself a reason to FAIL.",
        *(f"- {line}" for line in decisions),
    ])


DECISIONS_START = "<!-- foragent:lane-decisions -->"
DECISIONS_END = "<!-- /foragent:lane-decisions -->"


def lane_decisions_block(section: str) -> str:
    """`section` between markers, so a later review of the same PR can replace it."""
    return f"{DECISIONS_START}\n{section}\n{DECISIONS_END}" if section else ""


def refresh_lane_decisions(body: str, section: str) -> str:
    """Replace a PR body's lane-decisions block with `section` (append, or drop when empty)."""
    import re

    block = lane_decisions_block(section)
    pattern = re.compile(rf"(?:\n\n)?{re.escape(DECISIONS_START)}.*?{re.escape(DECISIONS_END)}", re.S)
    if pattern.search(body):
        return pattern.sub(lambda match: f"\n\n{block}" if block else "", body, count=1)
    return f"{body}\n\n{block}" if block else body


def moved_files_section(moved_files: list[str]) -> str:
    return ("The base branch moved or split files this branch edits (old -> new). "
            "Carry your edits to the new locations so none are orphaned:\n"
            + "\n".join(f"- {line}" for line in moved_files))


def goal_condition(acceptance_dir: str) -> str:
    prefix = 'Every command in the "Done when" list below exits 0 when run from the repository root, nothing under '
    suffix = (" is modified, and you did not edit files outside this repository."
              " Files that test tooling writes outside the repository do not count as your edits."
              " At 40 turns, end the run without repeating the stop check.")
    return prefix + acceptance_dir[:3500 - len(prefix) - len(suffix)] + suffix


BUG_FIX_REFERENCE = "skills/fa-orchestrator/references/bug-fix.md"
# The D79 skeleton, adapted from pstack/playbooks/bug-fix.md (MIT); the full text is BUG_FIX_REFERENCE.
BUG_FIX_STEPS = (
    "This issue is a bug. Follow these steps:\n"
    "1. Reproduce the bug on the surface where it was reported before changing anything.\n"
    "2. Rule out hypotheses with runtime evidence until one survives. Do not guess; when the state is "
    "unclear, add logging or instrumentation and read its output.\n"
    "3. Plan the smallest fix of the root cause that the evidence justifies; revert changes "
    "that refuted hypotheses motivated.\n"
    "4. Confirm the bug at the same place as the reproduction after the fix.\n"
    "5. Write a failing regression test first, and see it fail before you change the code.\n"
    "6. Report the root cause and the evidence (failing-then-passing output) in your final message.\n"
)


def is_bug_label(label: str) -> bool:
    name = label.strip().lower()
    return name == "bug" or name.endswith((":bug", " bug"))


def bug_fix_steps(labels: Iterable[str], worktree: Path) -> str | None:
    """Return the bug-fix summary for a bug-labelled issue, else None."""
    if not any(is_bug_label(label) for label in labels):
        return None
    if (worktree / BUG_FIX_REFERENCE).is_file():
        return BUG_FIX_STEPS + f"The full procedure is in `{BUG_FIX_REFERENCE}`.\n"
    return BUG_FIX_STEPS


def build_prompt(
    n: int, parsed: ParsedIssue, acceptance_dir: str, failure: str = "", language: str = "en",
    *, native_goal: bool = False, acceptance_locked: bool = False,
    merge_conflicts: list[str] | None = None,
    reviewer_text: str | None = None,
    orchestrator_hint: str | None = None,
    test_command: str | None = None,
    acceptance_baseline: tuple[str, ...] = (),
    failure_limit: int | None = 4000,
    skill_catalog: str = "",
    previous_attempt: str = "",
    review_feedback: bool = False,
    retry_default: bool = False,
    parent_context: str = "",
    full_text_dir: Path | None = None,
    moved_files: list[str] | None = None,
    procedure: str | None = None,
    lane_plan: str = "",
) -> str:
    from foragent import runner

    previous_attempt, reviewer_text, orchestrator_hint = _without_notes(
        previous_attempt, reviewer_text, orchestrator_hint)
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    acceptance_rule = "" if acceptance_locked else f"- Do not modify anything under {acceptance_dir}. Those tests are fixed.\n"
    test_rule = (
        "- The runner runs Done when, the previously passing acceptance tests and related unit tests. "
        f"CI runs the full suite after publication (`{test_command}`) and sends failures back for repair. "
        if test_command is not None else "- The runner runs Done when. "
    )
    test_rule += (
        "You run only the Done when tests and the tests for files you change; no parallel test helper is needed. "
        "Run tests with `fa test <pytest args>`; it waits for a machine-wide test slot.\n"
    )
    prompt = (
        f"You are implementing GitHub issue #{n} in this repository.\n"
        f"{parsed.goal}\n"
        + (parent_section(parent_context) + "\n" if parent_context else "")
        + f"Out of scope (do not do these): {parsed.out_of_scope}\n"
        "The work is accepted only when these commands succeed (they will be run for you afterwards):\n"
        f"{commands}\n"
        "Rules:\n"
        f"{acceptance_rule}"
        f"{test_rule}"
        f"- {IMPLEMENTER_TEST_RULE}\n"
        "- If the issue needs a product decision you cannot make from the text and the code, do not guess:\n"
        "  write .foragent/QUESTION.md with the question, 2–3 options and your recommendation, then stop.\n"
        f"  Write it in {language}. Include an `## Evidence` section quoting the exact conflicting lines "
        "(issue sentence; file:line and the code line).\n"
        "- Do not commit, push or switch branches. Leave your changes in the working tree.\n"
        "- For each reviewer point either fix it or write `.foragent/REBUTTAL.md` "
        "with `- <point>: <why it is out of scope or not a defect>`.\n"
        "- When the Done when commands would pass, also write .foragent/TRY.md so a person can check your work in about a minute.\n"
        f"  Write the prose in {language}. Keep these English headings exactly:\n"
        "  ## What changed\n"
        "  (one or two sentences, user-visible terms, no code jargon)\n"
        "  ## Try it\n"
        "  kind: cli            (or: web)\n"
        "  ```sh\n"
        "  <commands a person runs; for cli, commands that print something meaningful>\n"
        "  ```\n"
        "  ## Expect\n"
        "  (what a correct result looks like, concretely)\n"
        "  For kind: web, also add:\n"
        "  ## Web\n"
        "  start: <command that starts the app>\n"
        "  port: <port>\n"
        "  path: <path to open>\n"
        "  steps:\n"
        "  - <what to click or type>\n"
    )
    if procedure is not None:
        prompt += "\n" + procedure
    if lane_plan:
        prompt += "\n" + lane_plan + "\n"
    if previous_attempt:
        prompt += "\nPrevious attempt (from local records):\n" + previous_attempt + "\n"
    if reviewer_text is not None:
        prompt += "\nA reviewer said (verbatim):\n" + reviewer_text
    elif failure:
        prompt += "\nA previous attempt failed. Output of the failing check:\n" + (
            failure if failure_limit is None else cuts.tail(failure, failure_limit, full_text_dir, "failure.txt")
        )
    if review_feedback:
        prompt += "\n" + REVIEW_FEEDBACK_INSTRUCTIONS + "\n" + SEARCHED_LIST_INSTRUCTIONS + "\n"
    if acceptance_baseline:
        prompt += (
            f"\nThese acceptance tests already pass on the base and must keep passing ({len(acceptance_baseline)}):\n"
            + "\n".join(f"- {node_id}" for node_id in acceptance_baseline)
        )
    if merge_conflicts is not None:
        prompt += (
            "\n\nResolve the conflict markers in the following files:\n"
            + "\n".join(f"- {path}" for path in merge_conflicts)
            + "\nPreserve both the features newly added on the base branch and the features for this issue. "
            "Do not commit. The runner will stage the resolution and create the merge commit."
        )
    if moved_files:
        prompt += "\n\n" + moved_files_section(moved_files)
    prompt += skill_catalog
    if orchestrator_hint is not None:
        prompt = "Orchestrator hint:\n" + orchestrator_hint + "\n\n" + prompt
    if retry_default:
        # A queued retry's default always comes before any orchestrator hint.
        prompt = "Retry instructions:\n" + DEFAULT_RETRY_HINT + "\n" + SEARCHED_LIST_INSTRUCTIONS + "\n\n" + prompt
    return f"/goal {runner.goal_condition(acceptance_dir)}\n\n{prompt}" if native_goal else prompt


def _without_notes(*texts: str | None) -> tuple[str | None, ...]:
    """Feedback channels without reviewer NOTE lines: notes are recorded, never work to do."""
    from foragent import review_notes

    return tuple(None if text is None else review_notes.strip(text) for text in texts)


def build_resume_prompt(
    n: int, previous_body: str | None, body: str, failure: str = "", *,
    reviewer_text: str | None = None, orchestrator_hint: str | None = None,
    merge_conflicts: list[str] | None = None, review_feedback: bool = False, retry_default: bool = False,
    moved_files: list[str] | None = None, parent_context: str = "", procedure: str | None = None,
    lane_plan: str = "",
) -> str:
    """Deliver exact specification edits and feedback to an existing session."""
    reviewer_text, orchestrator_hint = _without_notes(reviewer_text, orchestrator_hint)
    parts = [f"Continue implementing GitHub issue #{n} in this repository.", IMPLEMENTER_TEST_RULE]
    if parent_context:
        # Always restated: the session may predate a newly read or re-approved parent text.
        parts.append(parent_section(parent_context))
    if lane_plan:
        # Always restated: a newer preread and approval may replace the plan the session saw.
        parts.append(lane_plan)
    if previous_body is not None and previous_body != body:
        old, new = previous_body.splitlines(), body.splitlines()
        for tag, start, end, new_start, new_end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            location = f"{start + 1}-{end}" if start != end else f"after {start}"
            parts.append(f"Specification update at previous issue body lines {location}:\n" + (
                "\n".join(new[new_start:new_end]) if new_start != new_end else
                "Remove these previous requirements:\n" + "\n".join(old[start:end])
            ))
    if procedure is not None:
        parts.append(procedure.rstrip("\n"))
    if reviewer_text is not None:
        parts.append("A reviewer said (verbatim):\n" + reviewer_text)
    elif failure:
        parts.append("A previous attempt failed. Output or stop reason:\n" + failure)
    if review_feedback:
        parts.append(REVIEW_FEEDBACK_INSTRUCTIONS + "\n" + SEARCHED_LIST_INSTRUCTIONS)
    if retry_default:
        parts.append("Retry instructions:\n" + DEFAULT_RETRY_HINT + "\n" + SEARCHED_LIST_INSTRUCTIONS)
    if orchestrator_hint is not None:
        parts.append("Orchestrator hint:\n" + orchestrator_hint)
    if merge_conflicts is not None:
        parts.append("Resolve the conflict markers in these files:\n" + "\n".join(merge_conflicts))
    if moved_files:
        parts.append(moved_files_section(moved_files))
    return "\n\n".join(parts)
