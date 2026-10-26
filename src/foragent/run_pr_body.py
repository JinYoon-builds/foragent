"""Render the reviewed checks and implementation notes in a pull request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from foragent import risks
from foragent.run_prompts import review_scope_rules

if TYPE_CHECKING:
    from foragent.issue import ParsedIssue
    from foragent.run_checks import CheckResult
    from foragent.run_review import ReviewResult


def _pr_body(
    n: int, parsed: ParsedIssue, checks: list[CheckResult], attempt: int,
    reviews: list[ReviewResult], try_text: str | None = None,
    *, outside_lane: list[str] | None = None, fallback_model: str | None = None,
    attempt_tiers: dict[str, str] | None = None,
    missing_skills: list[str] | None = None,
    skills_line: str | None = None,
    rebuttals: str = "",
    review_scope: str = "claude-local", acceptance_dir: str = "tests/acceptance/",
    risk_notes: str = "", language: str = "en",
) -> str:
    lines = [f"Closes #{n}", "", "| Done when command | Result |", "| --- | --- |"]
    for check in checks:
        command = check.command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        command = command.replace("`", "&#96;").replace("\n", "<br>")
        passed = check.exit_code == 0 and not check.timed_out
        lines.append(f"| <code>{command}</code> | {'pass' if passed else 'fail'} |")
    lines.extend(["", "## Review", ""])
    if review_scope == "pr-body":
        lines.append(f"VERDICT: {reviews[-1].verdict}" if reviews else "Pending review.")
        lines.extend(["", "## Review scope", "", review_scope_rules(acceptance_dir, language),
                      "The acceptance tests are locked."])
        if risk_notes:
            lines.extend(["", risks.notes_block(risk_notes)])
    elif not reviews:
        lines.append("Disabled (reviewer: none).")
    for round_number, review in enumerate(reviews if review_scope == "claude-local" else [], 1):
        lines.append(f"Round {round_number}: VERDICT: {review.verdict}")
        if review.findings:
            lines.append(review.findings)
        lines.extend(review.notes)
        lines.append("")
    if rebuttals:
        lines.extend(["", "## Rebuttals", "", rebuttals])
    if parsed.done_when_checklist:
        lines.extend(["", "## Needs a human check", ""])
        lines.extend(f"- [ ] {item.text}" for item in parsed.done_when_checklist)
    if outside_lane:
        lines.extend(["", "## Outside lane", "", *(f"- `{path}`" for path in outside_lane)])
    if missing_skills:
        lines.extend(["", "## Lane skills", "", *(f"- missing skill: {name}" for name in missing_skills)])
    if skills_line is not None:
        lines.extend(["", skills_line])
    if fallback_model is not None:
        lines.extend(["", f"Executor fallback model: {fallback_model}"])
    if attempt_tiers:
        lines.extend(["", *(f"Attempt {number} at tier {tier}" for number, tier in attempt_tiers.items())])
    lines.extend(["", f"Opened by Foragent gate (attempt {attempt})"])
    return "\n".join(lines) + "\n\n## Try it\n\n" + (
        try_text if try_text is not None else "_not provided by the implementer_"
    )

