"""Render the reviewed checks and implementation notes in a pull request."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from foragent import risks
from foragent.pr_body_limit import PrBody, Quoted, join_pr_body, short_section_end
from foragent.run_prompts import lane_decisions_block, review_scope_rules

if TYPE_CHECKING:
    from foragent.issue import ParsedIssue
    from foragent.run_checks import CheckResult
    from foragent.run_review import ReviewResult


_SUMMARY_HEADING = re.compile(r"^## What changed[ \t]*\n", re.MULTILINE)


def _try_parts(try_text: str) -> list[str]:
    """The card is the implementer's text, apart from the summary a shortened body keeps."""
    match = _SUMMARY_HEADING.search(try_text)
    if match is None:
        return [Quoted(try_text, "Try card")]
    end = short_section_end(try_text, match.end())
    return [Quoted(try_text[:match.start()], "Try card"), try_text[match.start():end], Quoted(try_text[end:], "Try card")]


def _pr_body(
    n: int, parsed: ParsedIssue, checks: list[CheckResult], attempt: int,
    reviews: list[ReviewResult], try_text: str | None = None,
    *, outside_lane: list[str] | None = None, fallback_model: str | None = None,
    attempt_tiers: dict[str, str] | None = None,
    missing_skills: list[str] | None = None,
    skills_line: str | None = None,
    rebuttals: str = "", searched: str = "",
    review_scope: str = "claude-local", acceptance_dir: str = "tests/acceptance/",
    risk_notes: str = "", language: str = "en", lane_decisions: str = "",
) -> PrBody:
    table = ["| Done when command | Result |", "| --- | --- |"]
    for check in checks:
        command = check.command.replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;")
        command = command.replace("`", "&#96;").replace("\n", "<br>")
        passed = check.exit_code == 0 and not check.timed_out
        table.append(f"| <code>{command}</code> | {'pass' if passed else 'fail'} |")
    lines = [f"Closes #{n}", "", Quoted("\n".join(table), "Done when table"), "", "## Review", ""]
    if review_scope == "pr-body":
        lines.append(f"VERDICT: {reviews[-1].verdict}" if reviews else "Pending review.")
        lines.extend(["", "## Review scope", "", review_scope_rules(acceptance_dir, language),
                      "The acceptance tests are locked."])
        if risk_notes:
            lines.extend(["", risks.notes_block(risk_notes)])
        if lane_decisions:
            # Native PR review reads only the PR body, so the lane's DECIDE lines travel here.
            lines.extend(["", lane_decisions_block(lane_decisions)])
    elif not reviews:
        lines.append("Disabled (reviewer: none).")
    for round_number, review in enumerate(reviews if review_scope == "claude-local" else [], 1):
        lines.append(f"Round {round_number}: VERDICT: {review.verdict}")
        if review.findings:
            lines.append(Quoted(review.findings, f"Round {round_number} findings"))
        if notes := [*(f"NOTE: {note}" for note in review.review_notes), *review.notes]:
            lines.append(Quoted("\n".join(notes), f"Round {round_number} notes"))
        lines.append("")
    if rebuttals:
        lines.extend(["", "## Rebuttals", "", Quoted(rebuttals, "Rebuttals")])
    if searched:
        # The lane's own list of what it searched for, where and what it found; read, never judged.
        lines.extend(["", "## Searched", "", Quoted(searched, "Searched")])
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
    return join_pr_body([
        join_pr_body(lines, "\n"), "\n\n## Try it\n\n",
        *(["_not provided by the implementer_"] if try_text is None else _try_parts(try_text)),
    ])

