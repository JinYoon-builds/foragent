"""Implementer and reviewer prompts, review parsing and PR bodies."""

from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
from foragent.git import GitRepo, REVIEW_SCOPE_FILE
from foragent.issue import ParsedIssue
from foragent.runner_core import CheckResult


REVIEW_PROMPT = (
    "Review this change for defects that break the stated goal, violate Out of scope, "
    "or would make the Done when commands pass without the goal being met "
    "(weakened checks, special-casing test inputs). Ignore style and nice-to-haves. "
    "Do not edit files. Reply with exactly one line `VERDICT: PASS` or `VERDICT: FAIL` "
    "followed by at most 5 findings, each one line with file:line and the concrete failure."
)


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    findings: str
    notes: tuple[str, ...] = ()
    raw: str = ""
    timed_out: bool = False


def review_scope_rules(acceptance_dir: str) -> str:
    return (
        "Block only on: the Goal is not met, something listed in Out of scope was changed, or a check was weakened.\n"
        f"Tests under {acceptance_dir} that belong to other issues fail on purpose until those issues land; do not report them.\n"
        "Improvements beyond this issue's Goal are not defects."
    )


def build_review_prompt(
    parsed: ParsedIssue, diff: str, acceptance_dir: str = "tests/acceptance/", *, rebuttals: str = "",
) -> str:
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    return (
        f"{REVIEW_PROMPT}\n\n{review_scope_rules(acceptance_dir)}"
        f"\n\nGoal:\n{parsed.goal}\n\nOut of scope:\n{parsed.out_of_scope}"
        f"\n\nDone when commands:\n{commands}\n\nDiff:\n{diff[:60000]}"
        + (f"\n\nImplementer rebuttals (judge them):\n{rebuttals}" if rebuttals else "")
    )


def parse_review(message: str) -> ReviewResult:
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if match := re.fullmatch(r"VERDICT: (PASS|FAIL)\s*", line):
            findings = [line for line in lines[index + 1:] if line.strip()][:5]
            return ReviewResult(match[1], "\n".join(findings))
    return ReviewResult("UNUSABLE", "", ("reviewer gave no verdict",))


def parse_code_review(message: str) -> ReviewResult:
    try:
        event = json.loads(message)
    except ValueError:
        event = None
    if not isinstance(event, dict) or event.get("is_error") or not isinstance(event.get("result"), str):
        return ReviewResult("UNUSABLE", "", ("/code-review gave no usable result",))
    text = event["result"]
    lines = [line for line in text.splitlines() if line.strip()]
    findings = [line for line in lines if re.match(r"^(?:[-*]|\d+[.)])?\s*`?\S+?:\d+`?\s+(—|-|--)\s+", line)]
    if findings:
        return ReviewResult("FAIL", "\n".join(findings))
    payload = text.strip()
    blocks = list(re.finditer(r"^[ \t]*```([^\r\n]*)\r?\n(.*?)^[ \t]*```[ \t]*\r?$", payload, re.M | re.S))
    json_blocks = [block for block in blocks if block[1].strip() == "json"]
    if len(json_blocks) > 1:
        return ReviewResult("UNUSABLE", "", ("/code-review gave multiple findings payloads",))
    if len(blocks) == 1 and blocks[0].span() == (0, len(payload)):
        payload = blocks[0][2]
    elif json_blocks:
        payload = json_blocks[0][2]
    try:
        items = json.loads(payload)
    except ValueError:
        items = None
    if isinstance(items, list):
        if not items:
            return ReviewResult("PASS", "")
        if any(not isinstance(item, dict) or not {"file", "line", "summary"}.intersection(item) for item in items):
            return ReviewResult("UNUSABLE", "", ("/code-review gave invalid findings",))
        return ReviewResult("FAIL", "\n".join(
            f"{item.get('file', '?')}:{item.get('line', '?')} — {item.get('summary', '')}" for item in items
        ))
    if not blocks and lines and lines[0].strip() == "(none)":
        return ReviewResult("PASS", "")
    return ReviewResult("UNUSABLE", "", ("/code-review gave no verdict",))


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
) -> str:
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
        f"Out of scope (do not do these): {parsed.out_of_scope}\n"
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
            failure if failure_limit is None else failure[-failure_limit:]
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
    if orchestrator_hint is not None:
        prompt = "Orchestrator hint:\n" + orchestrator_hint + "\n\n" + prompt
    return f"/goal {goal_condition(acceptance_dir)}\n\n{prompt}" if native_goal else prompt


def build_resume_prompt(
    n: int, previous_body: str | None, body: str, failure: str = "", *,
    reviewer_text: str | None = None, orchestrator_hint: str | None = None,
    merge_conflicts: list[str] | None = None,
) -> str:
    """Deliver exact specification edits and feedback to an existing session."""
    parts = [f"Continue implementing GitHub issue #{n} in this repository."]
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
    return "\n\n".join(parts)


def _pr_body(
    n: int, parsed: ParsedIssue, checks: list[CheckResult], attempt: int,
    reviews: list[ReviewResult], try_text: str | None = None,
    *, outside_lane: list[str] | None = None, fallback_model: str | None = None,
    attempt_tiers: dict[str, str] | None = None,
    missing_skills: list[str] | None = None,
    rebuttals: str = "",
    review_scope: str = "claude-local", acceptance_dir: str = "tests/acceptance/",
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
        lines.extend(["", "## Review scope", "", review_scope_rules(acceptance_dir),
                      "The acceptance tests are locked."])
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
    if fallback_model is not None:
        lines.extend(["", f"Executor fallback model: {fallback_model}"])
    if attempt_tiers:
        lines.extend(["", *(f"Attempt {number} at tier {tier}" for number, tier in attempt_tiers.items())])
    lines.extend(["", f"Opened by Foragent gate (attempt {attempt})"])
    return "\n".join(lines) + "\n\n## Try it\n\n" + (
        try_text if try_text is not None else "_not provided by the implementer_"
    )


@contextmanager
def _claude_review_scope(git: GitRepo, body: str, acceptance_dir: str, rebuttals: str = ""):
    git.ignore_review_scope()
    path = git.root / REVIEW_SCOPE_FILE
    link = os.readlink(path) if path.is_symlink() else None
    original = path.read_bytes() if path.exists() else None
    mode = path.stat().st_mode if path.exists() else None
    section = (
        "# Review scope (written by Foragent for this review only)\n\n"
        f"{body}\n\n{review_scope_rules(acceptance_dir)}\n\n"
        + (f"Implementer rebuttals (judge them):\n{rebuttals}\n\n" if rebuttals else "")
    ).encode("utf-8")
    try:
        if link is not None:
            path.unlink()
        path.write_bytes(section + (original if original is not None else b""))
        yield
    finally:
        if path.is_symlink():
            path.unlink()
        if link is not None:
            path.unlink(missing_ok=True)
            path.symlink_to(link)
        elif original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
            path.chmod(mode)


def _read_rebuttals(worktree: Path) -> str:
    path = worktree / ".foragent/REBUTTAL.md"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _normalized_findings(review: ReviewResult) -> set[str]:
    # Unstructured prose is one finding: repeated preambles alone are not a dispute.
    points = [review.raw] if review.verdict == "UNUSABLE" else review.findings.splitlines()
    return {" ".join(re.sub(r"^\s*[-*]\s+", "", point).split()).casefold()
            for point in points if point.strip()}
