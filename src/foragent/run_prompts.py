"""Implementation and review instructions built from the approved issue."""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path

from foragent import cuts
from foragent.issue import ParsedIssue


REVIEW_PROMPT = (
    "Review this change for defects that break the stated goal, violate Out of scope, "
    "or would make the Done when commands pass without the goal being met "
    "(weakened checks, special-casing test inputs). Ignore style and nice-to-haves. "
    "Do not edit files. Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` "
    "followed by every finding, each one line with file:line and the concrete failure."
)


def review_scope_rules(acceptance_dir: str) -> str:
    return (
        "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
        f"Tests under {acceptance_dir} that belong to other issues fail on purpose until those issues land; do not report them.\n"
        "Improvements beyond this issue's Goal are not defects."
    )


def build_review_prompt(
    parsed: ParsedIssue, diff: str, acceptance_dir: str = "tests/acceptance/", *, rebuttals: str = "",
    full_text_dir: Path | None = None,
) -> str:
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    return (
        f"{REVIEW_PROMPT}\n\n{review_scope_rules(acceptance_dir)}"
        f"\n\nGoal:\n{parsed.goal}\n\nOut of scope:\n{parsed.out_of_scope}"
        f"\n\nDone when commands:\n{commands}\n\nDiff:\n{cuts.head(diff, 60000, full_text_dir, 'review-diff.patch')}"
        + (f"\n\nImplementer rebuttals (judge them):\n{rebuttals}" if rebuttals else "")
    )


def parent_section(parent_context: str) -> str:
    return f"This issue is part of a larger order. Its shared outcome and constraints:\n{parent_context}"


def moved_files_section(moved_files: list[str]) -> str:
    return ("The base branch moved or split files this branch edits (old -> new). "
            "Carry your edits to the new locations so none are orphaned:\n"
            + "\n".join(f"- {line}" for line in moved_files))


def goal_condition(acceptance_dir: str) -> str:
    prefix = 'Every command in the "Done when" list below exits 0 when run from the repository root, nothing under '
    suffix = " is modified, and no files outside this repository are changed. Or stop after 40 turns."
    return prefix + acceptance_dir[:3500 - len(prefix) - len(suffix)] + suffix


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
    parent_context: str = "",
    full_text_dir: Path | None = None,
    moved_files: list[str] | None = None,
) -> str:
    from foragent import runner

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
    if reviewer_text is not None:
        prompt += "\nA reviewer said (verbatim):\n" + reviewer_text
    elif failure:
        prompt += "\nA previous attempt failed. Output of the failing check:\n" + (
            failure if failure_limit is None else cuts.tail(failure, failure_limit, full_text_dir, "failure.txt")
        )
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
    return f"/goal {runner.goal_condition(acceptance_dir)}\n\n{prompt}" if native_goal else prompt


def build_resume_prompt(
    n: int, previous_body: str | None, body: str, failure: str = "", *,
    reviewer_text: str | None = None, orchestrator_hint: str | None = None,
    merge_conflicts: list[str] | None = None, moved_files: list[str] | None = None,
    parent_context: str = "",
) -> str:
    """Deliver exact specification edits and feedback to an existing session."""
    parts = [f"Continue implementing GitHub issue #{n} in this repository."]
    if parent_context:
        # Always restated: the session may predate a newly read or re-approved parent text.
        parts.append(parent_section(parent_context))
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
    if reviewer_text is not None:
        parts.append("A reviewer said (verbatim):\n" + reviewer_text)
    elif failure:
        parts.append("A previous attempt failed. Output or stop reason:\n" + failure)
    if orchestrator_hint is not None:
        parts.append("Orchestrator hint:\n" + orchestrator_hint)
    if merge_conflicts is not None:
        parts.append("Resolve the conflict markers in these files:\n" + "\n".join(merge_conflicts))
    if moved_files:
        parts.append(moved_files_section(moved_files))
    return "\n\n".join(parts)
