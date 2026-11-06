"""Score each FAIL finding; the scorer supplies consequences, frequency and defect class.

The procedure (score table, false positives, grounding rule) lives in the review-procedure
skill. This module owns the flow and the structured reply only: one fresh scoring session
per finding, run one at a time with the reviewer, and fact checks on what the reply claims
(the quoted Goal line is in the Goal or Out of scope, the locked test file exists under the
acceptance directory, a breaking input is stated). A reply without a valid score keeps the
finding blocking. Scores record confidence; grounded consequences decide whether to block.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from foragent import orch_events, review_notes

if TYPE_CHECKING:
    from foragent.git import GitRepo
    from foragent.issue import ParsedIssue
    from foragent.run_context import RunContext
    from foragent.run_review import ReviewResult

FACTS = ("critical_path", "exists", "wrong_result", "ordinary_input", "irreversible")
FIELDS = ("score", *FACTS, "finding_class", "goal_line", "locked_test", "breaking_input", "spec_question")

TASK = (
    "Score the one review finding in the JSON object above, following the review-procedure skill: "
    "its 0-100 score table, its false-positive list and its grounding rule. You score; you do not "
    "review the change again and you do not edit files. The approved Goal and Out of scope are in "
    "the object. Reply with exactly one JSON object and nothing else, with these keys:\n"
    '- "score": integer 0-100 from the score table;\n'
    '- "critical_path": true when the finding is a fatal path (data loss, breaking main, a merge '
    "without approval, a person-only permission handed to an agent), else false; context only, "
    "it does not override the consequence facts below;\n"
    '- "exists": true when the reported failure really exists in this change, including when '
    'critical_path is false; else false;\n'
    '- "wrong_result": true for a wrong result, lost work, unverified code reaching main, a stalled '
    'gate or a broken locked test; false for slower, less tidy or wording alone;\n'
    '- "ordinary_input": true when the failure occurs on ordinary supported inputs, false when '
    'only rare inputs trigger it; explain the input in breaking_input;\n'
    '- "irreversible": true when the harm cannot be undone (work lost, data deleted, something '
    'sent outside), including on rare inputs; else false;\n'
    '- "finding_class": a nonempty name for the root-cause defect class. Reuse a name from '
    'previous_classes when it is the same class, even if wording, files or instances differ;\n'
    '- "goal_line": a line quoted exactly from the Goal or Out of scope that the finding breaks, or null;\n'
    '- "locked_test": a locked acceptance test this change makes fail, as path[::name] under the '
    "acceptance directory, or null;\n"
    '- "breaking_input": a concrete input or sequence that really breaks if this merges, or null;\n'
    '- "spec_question": when the Goal does not decide the point, the question for the planning side, else null.\n'
    'A ground alone or a high score does not block. A grounded, existing failure blocks only for '
    'irreversible harm or a wrong_result on ordinary_input. Rare reversible bugs and findings '
    'without wrong results pass with follow-up issues. Classify these facts yourself; the harness '
    'only validates and routes your assessment.'
)


def findings_of(text: str) -> list[str]:
    """One finding per unindented line, with the indented lines (evidence, affected) that follow it."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[:1].isspace() and blocks:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return ["\n".join(block) for block in blocks]


def parse_reply(text: str) -> dict[str, Any] | None:
    """The scorer's JSON object when every field has a valid value, else None."""
    body = text.strip()
    fenced = re.fullmatch(r"```[^\n]*\n(.*?)\n?```", body, re.S)
    if fenced:
        body = fenced[1].strip()
    try:
        reply = json.loads(body)
    except ValueError:
        return None
    if not isinstance(reply, dict):
        return None
    score = reply.get("score")
    if type(score) is not int or not 0 <= score <= 100:
        return None
    if any(type(reply.get(key)) is not bool for key in FACTS):
        return None
    if not isinstance(reply.get("finding_class"), str) or not reply["finding_class"].strip():
        return None
    if any(reply.get(key) is not None and not isinstance(reply.get(key), str)
           for key in ("goal_line", "locked_test", "breaking_input", "spec_question")):
        return None
    return {key: reply.get(key) for key in FIELDS}


def _words(text: str) -> str:
    return " ".join(text.split())


def grounds(reply: dict[str, Any], parsed: ParsedIssue, root: Path, acceptance_dir: str) -> list[str]:
    """The claimed grounds that check out as facts."""
    found = []
    quote = _words(reply["goal_line"] or "").strip("\"'`").strip()
    if quote and any(quote in _words(line) for section in (parsed.goal, parsed.out_of_scope)
                     for line in section.splitlines()):
        found.append("goal_line")
    if _locked_test_exists(reply["locked_test"] or "", root, acceptance_dir):
        found.append("locked_test")
    if (reply["breaking_input"] or "").strip():
        found.append("breaking_input")
    return found


def _locked_test_exists(claim: str, root: Path, acceptance_dir: str) -> bool:
    path, _, name = claim.strip().partition("::")
    directory = acceptance_dir.strip("/") + "/"
    if not path or not path.startswith(directory) or ".." in Path(path).parts:
        return False
    file = root / path
    if not file.is_file():
        return False
    name = name.split("[", 1)[0].split("::")[-1].strip()
    return not name or re.search(rf"\bdef {re.escape(name)}\b", file.read_text(errors="replace")) is not None


def outcome(reply: dict[str, Any] | None, found: list[str]) -> str:
    """blocks, note or spec-question; an unusable reply keeps the finding blocking."""
    if reply is None:
        return "unusable"
    if (reply["spec_question"] or "").strip():
        return "spec-question"
    if not found or not reply["exists"]:
        return "note"
    return "blocks" if reply["irreversible"] or (reply["wrong_result"] and reply["ordinary_input"]) else "note"


def failed_rounds(n: int, ctx: RunContext) -> list[dict]:
    """Failed rounds in the current approved scope, after its latest PASS."""
    approval = ctx.store.load_approval(n)
    rounds = {}
    for event in orch_events.events(ctx.store, "review"):
        if event.get("issue") != n or (approval and event.get("body_sha") != approval.body_sha):
            continue
        if event.get("verdict") == "PASS":
            rounds.clear()
        elif event.get("verdict") == "FAIL":
            rounds[event["round"]] = event
    return list(rounds.values())


def repeated_classes(n: int, ctx: RunContext) -> list[str]:
    """Classes the agent reported in two distinct failed rounds; no prose classification here."""
    counts = {}
    for event in failed_rounds(n, ctx):
        for name in set(event.get("finding_classes", [])):
            counts[name] = counts.get(name, 0) + 1
    return sorted(name for name, count in counts.items() if count >= 2)


def _followup(n: int, ctx: RunContext, finding: str, reply: dict[str, Any]) -> int:
    """One tracker issue per source finding, reusable after a retry or lost local receipt."""
    digest = hashlib.sha256(_words(finding).encode("utf-8")).hexdigest()
    marker = f"<!-- foragent:review-followup source={n} finding={digest} -->"
    for issue in ctx.tracker.list_issues():
        if marker in issue.body:
            return issue.number
    source = ctx.tracker.get_issue(n)
    labels = sorted(label for label in source.labels if label.startswith(("by:", "lane:")))
    body = (f"{marker}\nSource issue: #{n}\n\n## Goal\nAddress this non-blocking review finding.\n\n"
            f"{finding}\n\n## Review assessment\n{json.dumps(reply, ensure_ascii=False)}\n")
    title = f"Review follow-up for #{n}: {finding.splitlines()[0]}"[:256]
    return ctx.tracker.create_issue(title, body, labels)


def score(n: int, ctx: RunContext, review: ReviewResult, round_number: int, *,
          git: GitRepo | None, parsed: ParsedIssue | None, record: bool = True) -> ReviewResult:
    """Score every finding of a FAIL; keep the blocking ones, record the rest as NOTE with its score."""
    from foragent import runner
    from foragent.config import resolved_reviewer
    from foragent.run_review import SPEC_CONFLICT, _append, _available_executor, _raw_text, _save_review

    if (review.verdict != "FAIL" or review.conflicts or git is None or parsed is None
            or not getattr(ctx.config, "review_scoring", True)):
        return review
    findings = findings_of(review.findings)
    if not findings:
        return review
    reviewer = resolved_reviewer(ctx.config)
    executor = ctx.executor_factory(reviewer)
    acceptance = ctx.config.acceptance_dir
    blocking, notes, questions, classes = [], [], [], []
    previous_classes = sorted({name for event in failed_rounds(n, ctx)
                               for name in event.get("finding_classes", [])})
    edited = False
    for k, finding in enumerate(findings, 1):
        request = json.dumps({"finding": finding, "goal": parsed.goal, "out_of_scope": parsed.out_of_scope,
                              "acceptance_dir": acceptance, "round": round_number,
                              "previous_classes": previous_classes}, ensure_ascii=False)
        prompt = f"{request}\n\nGoal:\n{parsed.goal}\n\nOut of scope:\n{parsed.out_of_scope or '(none)'}\n\n{TASK}"
        name = f"score-{round_number}-{k}"
        # The scorer runs in the worktree like a reviewer: undo any edit before the fact checks read it.
        before = git.review_state()
        try:
            result = _available_executor(
                n, ctx, executor, worktree=git.root, reviewer=True, prompt=prompt, model=ctx.config.reviewer_model,
                timeout_s=runner._remaining(n, ctx), log_path=ctx.store.root / "runs" / f"issue-{n}" / f"{name}.log",
            )
        finally:
            if git.review_state() != before:
                git.restore_review_state(before)
                edited = True
                _append(ctx.store.log_path(n),
                        f"Scorer changed repository state in {name}; restored HEAD, index and working tree.")
        message = result.full_message if result.full_message is not None else result.last_message
        raw = _raw_text(reviewer, message or "")
        _save_review(n, ctx, f"{name}.txt", raw)
        reply = None if result.timed_out or result.exit_code != 0 else parse_reply(raw)
        found = grounds(reply, parsed, git.root, acceptance) if reply else []
        decided = outcome(reply, found)
        if record:
            orch_events.append_event(
                ctx.store, ctx.now(), n, "review-score", f"{decided} score={reply['score'] if reply else '-'}",
                round=round_number, finding=finding, outcome=decided, grounds=found,
                assessment=reply if reply is not None else {"unusable": raw[-2000:]},
            )
        if decided in ("blocks", "unusable"):
            blocking.append(finding)
            if reply is not None:
                classes.append(reply["finding_class"].strip())
        elif decided == "spec-question":
            question = _words(reply["spec_question"])
            questions.append(f"{SPEC_CONFLICT} Goal question: {question} (finding: {finding.splitlines()[0]})")
        else:
            followup = _followup(n, ctx, finding, reply) if record else None
            reference = f", follow-up #{followup} from #{n}" if followup is not None else ""
            notes.append(f"score {reply['score']}, not blocking{reference}: {_words(finding)}")
    if record:
        review_notes.record(ctx.store, ctx.now(), n, notes, round=round_number)
    return replace(review, verdict="FAIL" if blocking else "PASS", findings="\n".join(blocking),
                   finding_classes=tuple(dict.fromkeys(classes)),
                   conflicts=tuple(questions), review_notes=(*review.review_notes, *notes),
                   notes=(*review.notes, f"scored {len(findings)} findings: {len(blocking)} blocking",
                          *(["Scorer changes discarded; staged index restored."] if edited else [])))
