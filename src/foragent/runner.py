"""Execute, independently check and review one approved snapshot."""

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from itertools import islice
import ast
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import threading
import time
import traceback
import xml.etree.ElementTree as ET

import yaml

from foragent import board, orch_github
from foragent.actions import flag, invalidate_approval
from foragent.config import Config, resolved_reviewer
from foragent.executors import ExecResult, Executor, classify_result, executor_for, stream_session_id
from foragent.git import GitError, GitRepo, REVIEW_SCOPE_FILE
from foragent.issue import ParsedIssue, body_hash, has_machine_check, parse_body
from foragent.kick import kick_tick
from foragent.labels import APPROVED, BLOCKED, NEEDS_HUMAN, READY, RUNNING, attempt_tier, issue_lane, issue_size
from foragent.order import detect_test_commands
from foragent.orch_reinstall import after_merge
from foragent.policy import merge_decision, review_mode_for, touched
from foragent.processes import run_logged, track_process_group
from foragent.reasons import Reason
from foragent.skills import SkillManager, copy_lane_skills
from foragent.state import Approval, StateStore
from foragent.tracker import Tracker, TrackerError


def poll_executor[T](execute: Callable[[], T], poll: Callable[[], None]) -> T:
    """Keep execution on the main thread so subprocess termination still works."""
    stopped = threading.Event()

    def watch():
        while not stopped.wait(30):
            poll()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        return execute()
    finally:
        stopped.set()
        watcher.join()


@dataclass
class RunContext:
    repo: str
    repo_root: Path
    tracker: Tracker
    store: StateStore
    config: Config
    git: GitRepo
    now: Callable[[], datetime]
    executor_factory: Callable[[str], Executor] = executor_for
    sleep: Callable[[float], None] = time.sleep
    poll_hook: Callable[[Callable[[], ExecResult], Callable[[], None]], ExecResult] = poll_executor
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class CheckResult:
    command: str
    exit_code: int | None
    timed_out: bool
    output: str


@dataclass(frozen=True)
class AcceptanceBaseline:
    command: str
    node_ids: tuple[str, ...]


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


class PatchReplayError(GitError):
    """A child patch could not be replayed onto its selected destination."""


class AttemptFailure(Exception):
    """Checks on a refreshed base failed and need another implementation attempt."""


class ConflictBudgetExhausted(Exception):
    """Conflict resolution needs one fresh implementation on the latest target."""


class CIFailure(AttemptFailure):
    def __init__(self, logs: str, target_branch: str):
        super().__init__(f"CI failed:\n{logs}")
        self.target_branch = target_branch


class ExecutorStopped(Exception):
    def __init__(self, outcome: str, detail: str = ""):
        super().__init__(detail)
        self.outcome = outcome


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
        f"- Optional additional check: Before you finish, also run the full test suite: `{test_command}`. "
        "Each attempt checks Done when, the acceptance baseline and related unit tests; "
        "CI runs the full suite after publication and sends failures back for repair.\n"
        if test_command is not None else ""
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


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def _check(
    command: str, worktree: Path, log: Path, timeout_s: int,
    *, progress: Callable[[], None] | None = None,
) -> CheckResult:
    _append(log, f"\nDone when command:\n{command}")
    offset = log.stat().st_size

    def execute():
        return run_logged(
            ["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log,
        )

    # A quiet check is still running; keep the scheduler informed while it waits.
    code, timed_out, _ = poll_executor(execute, progress) if progress is not None else execute()
    with log.open("rb") as stream:
        stream.seek(max(offset, log.stat().st_size - 16000))
        tail = stream.read().decode("utf-8", errors="replace")[-4000:]
    _append(log, f"\nCheck exit_code={code}, timed_out={timed_out}\nOutput tail:\n{tail}")
    return CheckResult(command, code, timed_out, tail)


def _pytest_command(command: str) -> str | None:
    """Keep the pytest invocation/options, replacing its test selection later."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    index = next((i for i, word in enumerate(words)
                  if Path(word).name == "pytest"), None)
    if index is None or any(word and all(char in "();<>|&" for char in word) for word in words):
        return None
    # A full-suite command can exclude acceptance tests or select other paths.
    # Neither that selection nor early stopping should narrow the baseline.
    selection_values = {"--ignore", "--ignore-glob", "--deselect", "-k", "-m", "--maxfail", "--junitxml", "--junit-xml"}
    selection_flags = {"-x", "--exitfirst", "--lf", "--last-failed", "--ff", "--failed-first",
                       "--sw", "--stepwise", "--sw-skip", "--stepwise-skip", "--collect-only", "--co"}
    option_values = {"-c", "--config-file", "-o", "--override-ini", "-p", "--rootdir", "--confcutdir",
                     "--basetemp", "--import-mode", "--tb", "--capture", "--color", "--code-highlight",
                     "--junit-prefix", "--verbosity", "--durations", "--durations-min"}
    result = words[:index + 1]
    args = iter(words[index + 1:])
    for arg in args:
        option = arg.split("=", 1)[0]
        if option in selection_values:
            if "=" not in arg:
                next(args, None)
        elif arg in selection_flags or arg == "--":
            continue
        elif option in option_values and "=" not in arg:
            result.append(arg)
            if (value := next(args, None)) is not None:
                result.append(value)
        elif arg.startswith("-"):
            result.append(arg)
        # Remaining positional arguments are test paths/node ids.
    # Preserve shell environment assignments before the executable.
    prefix = []
    while result and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", result[0], re.S):
        name, value = result.pop(0).split("=", 1)
        prefix.append(f"{name}={shlex.quote(value)}")
    # Stable, repository-relative JUnit paths are also valid pytest selectors.
    return " ".join([*prefix, shlex.join(result), "-o addopts= --rootdir=."])


def _passing_acceptance_ids(report: Path) -> tuple[str, ...]:
    """Legacy JUnit provides exact file paths; classname adds nested classes."""
    passed, failed = set(), set()
    for case in ET.parse(report).iter("testcase"):
        file, name = case.get("file"), case.get("name")
        if not file or not name:
            continue
        module = file.removesuffix(".py").replace("/", ".")
        classname = case.get("classname", "")
        classes = classname.removeprefix(module).lstrip(".").replace(".", "::")
        node_id = "::".join(part for part in (file, classes, name) if part)
        (failed if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")) else passed).add(node_id)
    return tuple(sorted(passed - failed))


def _related_unit_tests(worktree: Path, paths: list[str], acceptance_dir: str) -> tuple[str, ...]:
    """Select changed pytest files and direct static importers of changed modules."""
    acceptance = Path(acceptance_dir)
    tests = {
        path.relative_to(worktree).as_posix(): path
        for path in (worktree / "tests").rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and not path.relative_to(worktree).is_relative_to(acceptance)
    }
    selected = set(paths) & tests.keys()
    modules = set()
    for name in paths:
        path = Path(name)
        if path.suffix != ".py" or path.is_relative_to("tests") or path.is_relative_to(acceptance):
            continue
        parts = list(path.with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.add(".".join(parts))
    if not modules:
        return tuple(sorted(selected))
    for name, path in tests.items():
        if name in selected:
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=name)
        except (SyntaxError, UnicodeError):
            continue
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imports.add(node.module)
                imports.update(f"{node.module}.{alias.name}" for alias in node.names)
        if any(imported == module or imported.startswith(module + ".")
               for imported in imports for module in modules):
            selected.add(name)
    return tuple(sorted(selected))


def _capture_acceptance_baseline(
    n: int, ctx: RunContext, test_command: str | None, target_ref: str,
) -> AcceptanceBaseline | None:
    log = ctx.store.log_path(n)
    command = _pytest_command(test_command) if test_command else None
    if command is None:
        reason = "no test command is known" if test_command is None else f"not a supported pytest command: {test_command}"
        _append(log, f"Acceptance baseline skipped (count=0): {reason}")
        return None
    report = ctx.store.root / "runs" / f"issue-{n}-acceptance-baseline.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    # Use a fresh target checkout, including when repairing an existing PR's CI.
    # Tests can write files; discard them without touching the implementation.
    with tempfile.TemporaryDirectory(prefix="foragent-baseline-") as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, target_ref)
        try:
            if not (worktree / ctx.config.acceptance_dir).is_dir():
                _append(log, "Acceptance baseline skipped (count=0): acceptance directory does not exist")
                return None
            if _remaining(n, ctx) <= 0:
                raise ExecutorStopped(_timeout(n, ctx))
            _phase(n, ctx, "checking")
            baseline_command = (
                f"{command} {shlex.quote(ctx.config.acceptance_dir)} "
                f"-o junit_family=legacy --junit-prefix= --junitxml={shlex.quote(str(report.resolve()))}"
            )
            result = _check(baseline_command, worktree, log, min(900, _remaining(n, ctx)),
                            progress=lambda: _progress(n, ctx))
            _progress(n, ctx)
            try:
                node_ids = _passing_acceptance_ids(report)
            except (OSError, ET.ParseError) as exc:
                _append(log, f"Acceptance baseline count=0: no usable JUnit report ({exc})")
                node_ids = ()
            _append(log, f"Acceptance baseline count={len(node_ids)}, exit_code={result.exit_code}, timed_out={result.timed_out}")
            return AcceptanceBaseline(command, node_ids)
        finally:
            ctx.git.remove_worktree(worktree)


def _claude_run_dir(snapshot: Path) -> Path:
    return snapshot.parent.parent / "runs" / f"issue-{snapshot.stem}"


def _write_claude_settings(n: int, ctx: RunContext, worktree: Path) -> Path:
    snapshot = (ctx.store.root / "approvals" / f"{n}.json").resolve()
    directory = _claude_run_dir(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stop-block-count").write_text("0", encoding="utf-8")
    acceptance = Path(os.path.relpath(worktree / ctx.config.acceptance_dir, worktree)).as_posix().rstrip("/") + "/"
    command = shlex.join([
        sys.executable, "-m", "foragent.cli", "gate", "check-done", "--snapshot", str(snapshot),
        "--worktree", str(worktree.resolve()), "--acceptance-dir", ctx.config.acceptance_dir,
    ])
    settings = {
        "permissions": {"deny": [
            f"Edit({acceptance}**)", f"Write({acceptance}**)",
            "Bash(git commit:*)", "Bash(git push:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
            "Bash(git merge:*)",
        ]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command,
                                      "timeout": max(900, ctx.config.max_runtime_minutes * 60)}]}]},
    }
    path = directory / "claude-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def check_done(snapshot: Path, worktree: Path, acceptance_dir: str) -> int:
    """Stop hook: re-use independent checks, allowing at most eight blocked stops."""
    directory = _claude_run_dir(snapshot)
    directory.mkdir(parents=True, exist_ok=True)
    counter = directory / "stop-block-count"
    blocked = 0
    try:
        blocked = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
        if blocked >= 8:
            print("Done when: Stop hook block cap reached; runner verification will decide.")
            return 0
        approval = Approval(**json.loads(snapshot.read_text(encoding="utf-8")))
        parsed = parse_body(approval.body)
        if not has_machine_check(parsed):
            raise ValueError("Done when has no machine check")
        for command in parsed.done_when_commands:
            result = _check(command, worktree, directory / "check-done.log", 900)
            if result.exit_code != 0 or result.timed_out:
                counter.write_text(str(blocked + 1), encoding="utf-8")
                print(f"Done when failed: {command}\n{result.output[-2000:]}"
                      + ("\nCheck timed out." if result.timed_out else ""), file=sys.stderr)
                return 2
    except (OSError, ValueError, TypeError) as exc:
        # Malformed snapshots and check-launch failures must also block a stop.
        counter.write_text(str(blocked + 1), encoding="utf-8")
        print(f"Done when failed: {exc}", file=sys.stderr)
        return 2
    print(f"Done when: all {len(parsed.done_when_commands)} commands passed; acceptance lock: {acceptance_dir}.")
    return 0


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


def _remaining(n: int, ctx: RunContext) -> int:
    run = ctx.store.get_run(n)
    if run is None:
        raise KeyError(n)
    elapsed = (ctx.now() - datetime.fromisoformat(run.started_at)).total_seconds() - run.waited_seconds
    return max(0, int(ctx.config.max_runtime_minutes * 60 - elapsed))


def _timeout(n: int, ctx: RunContext) -> str:
    flag(ctx, n, Reason.TIMEOUT, "Too big for one session; split it in order")
    return "timeout"


def _hold_changed_head(n: int, ctx: RunContext, link: str) -> str:
    flag(ctx, n, Reason.RISK_HIGH, "PR head changed after verification", link)
    return "held"


def _ci_failure(n: int, ctx: RunContext, pr: int, head: str, target_branch: str) -> None:
    """Spend one implementation retry, preserving the raw logs from every run."""
    failure = CIFailure(ctx.tracker.pr_failed_logs(pr), target_branch)
    link = f"https://github.com/{ctx.repo}/pull/{pr}"
    if ctx.tracker.pr_head_sha(pr) != head:
        raise ExecutorStopped(_hold_changed_head(n, ctx, link))
    run = ctx.store.get_run(n)
    _append(ctx.store.log_path(n), f"Attempt {run.attempts} failed:\n{failure}")
    if run.attempts >= ctx.config.max_retries + 1:
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "CI failed after all attempts.", f"{failure}\n\n{link}")
        raise ExecutorStopped("ci-failed", str(failure))
    ctx.store.update_run(n, attempts=run.attempts + 1)
    raise failure


def _progress(n: int, ctx: RunContext) -> None:
    ctx.store.update_run(n, last_progress_at=ctx.now().astimezone(UTC).isoformat())


def _phase(n: int, ctx: RunContext, phase: str) -> None:
    if ctx.store.get_run(n).phase != phase:
        ctx.store.update_run(n, phase=phase, phase_started_at=ctx.now().astimezone(UTC).isoformat())


def _run_executor(n: int, ctx: RunContext, executor: Executor, **kwargs) -> ExecResult:
    log = kwargs["log_path"]
    size = log.stat().st_size if log.exists() else 0
    offset = size

    def poll():
        nonlocal size
        current = log.stat().st_size if log.exists() else 0
        if current > size:
            timestamp = ctx.now().astimezone(UTC).isoformat()
            ctx.store.update_run(n, last_progress_at=timestamp, last_activity_at=timestamp)
        size = current

    def execute():
        with track_process_group(lambda pgid: ctx.store.update_run(n, executor_pgid=pgid)):
            return executor.run(**kwargs)

    try:
        result = ctx.poll_hook(execute, poll)
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(offset, log.stat().st_size - 16000))
                result.log_tail = stream.read().decode("utf-8", errors="replace")[-4000:]
        return result
    finally:
        poll()
        _progress(n, ctx)


def wait_for_rate_limit(n: int, ctx: RunContext, error: TrackerError) -> bool:
    """Persist a resumable wait through the shared GitHub availability mechanism."""
    if not error.is_rate_limit:
        return False
    orch_github.wait_run(ctx, n, orch_github.pause(ctx, error))
    return True


def _executor_wait(n: int, ctx: RunContext, seconds: float) -> None:
    """Persist the wait before sleeping so tick cannot mistake it for a stall."""
    run = ctx.store.get_run(n)
    started = ctx.now()
    started_mono = ctx.monotonic()
    ctx.store.update_run(
        n, waited_seconds=run.waited_seconds + seconds,
        executor_waited_seconds=run.executor_waited_seconds + seconds,
        waiting_until=(started + timedelta(seconds=seconds)).astimezone(UTC).isoformat(),
        last_progress_at=started.astimezone(UTC).isoformat(),
    )
    try:
        ctx.sleep(seconds)
    finally:
        elapsed = max(0, ctx.monotonic() - started_mono)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + elapsed,
            executor_waited_seconds=run.executor_waited_seconds + elapsed,
            waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
        )


@contextmanager
def _merge_lock(n: int, ctx: RunContext):
    run = ctx.store.get_run(n)
    started = ctx.monotonic()

    def progress():
        now = ctx.now().astimezone(UTC)
        ctx.store.update_run(
            n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
            waiting_until=(now + timedelta(seconds=60)).isoformat(), last_progress_at=now.isoformat(),
        )

    # Keep tick informed during an unbounded blocking flock acquisition.
    with ExitStack() as stack:
        progress()
        try:
            poll_executor(lambda: stack.enter_context(ctx.store.merge_lock()), progress)
        finally:
            ctx.store.update_run(
                n, waited_seconds=run.waited_seconds + max(0, ctx.monotonic() - started),
                waiting_until=None, last_progress_at=ctx.now().astimezone(UTC).isoformat(),
            )
        yield


def _available_executor(
    n: int, ctx: RunContext, executor: Executor, *, reviewer: bool = False,
    session_body: str | None = None, resume_prompt: str | None = None, **kwargs,
) -> ExecResult:
    errors = 0
    full_prompt = kwargs["prompt"]

    def execute() -> ExecResult:
        log = kwargs["log_path"]
        offset = log.stat().st_size if log.exists() else 0
        try:
            return _run_executor(n, ctx, executor, **kwargs)
        except BaseException:
            # run_logged flushes stdout into the log during interruption cleanup.
            # Recover this call's lifecycle before run_issue finishes its history.
            if session_body is not None and not reviewer and log.exists():
                identity = stream_session_id(log, ctx.config.executor, offset=offset)
                if identity is not None:
                    current = ctx.store.get_run(n)
                    ctx.store.update_run(
                        n, session_id=identity, session_executor=ctx.config.executor,
                        session_ids=[*current.session_ids, identity], session_body=session_body,
                    )
            raise

    while True:
        run = ctx.store.get_run(n)
        if not reviewer and run.fallback_model is not None:
            kwargs["model"] = run.fallback_model
        kwargs["timeout_s"] = _remaining(n, ctx)
        session_id = (run.session_id if session_body is not None and resume_prompt is not None and not reviewer
                      and run.session_executor == ctx.config.executor else None)
        kwargs["prompt"] = resume_prompt if session_id is not None else full_prompt
        if session_id is not None:
            kwargs["resume_session_id"] = session_id
        else:
            kwargs.pop("resume_session_id", None)
        try:
            result = execute()
        except OSError as exc:
            if session_id is None:
                raise
            _append(kwargs["log_path"], f"Session resume could not launch; starting fresh: {exc}")
            result = None
        if session_id is not None and (result is None or (
            result.session_id is None and result.exit_code not in {None, 0}
            and not result.timed_out and classify_result(result) == "normal"
        )):
            if result is not None:
                _append(kwargs["log_path"], f"Session resume failed; starting fresh:\n{result.last_message}\n{result.log_tail}")
            session_id = None
            kwargs.pop("resume_session_id")
            kwargs["prompt"] = full_prompt
            kwargs["timeout_s"] = _remaining(n, ctx)
            result = execute()
        if session_body is not None and not reviewer:
            identity = result.session_id or session_id
            reason = (f"Executor exit_code={result.exit_code}, timed_out={result.timed_out}\n"
                      f"{result.last_message}\n{result.log_tail}" if result.exit_code != 0 or result.timed_out else "")
            run = ctx.store.update_run(
                n, session_id=identity, session_executor=ctx.config.executor,
                session_ids=[*run.session_ids, *([identity] if identity is not None else [])],
                session_body=(session_body if result.session_id is not None or (
                    result.exit_code == 0 and not result.timed_out
                ) else run.session_body), resume_reason=reason,
            )
            if reason:
                resume_prompt = (resume_prompt or "") + "\n\n" + reason
        kind = classify_result(result)
        if kind == "normal":
            return result
        if kind == "transient" and _remaining(n, ctx) <= 0:
            return replace(result, timed_out=True)
        if kind == "limit" and not reviewer:
            ctx.store.update_run(n, detail=result.last_message or result.log_tail or "Executor usage limit reached.")
            ctx.store.pause_lane(run.lane, ctx.now() + timedelta(minutes=ctx.config.limit_wait_minutes))
            ctx.tracker.add_labels(n, [READY])
            ctx.tracker.remove_labels(n, [RUNNING])
            board.sync_status(ctx, n, "Ready")
            raise ExecutorStopped("limit-wait")
        if kind == "transient" and not reviewer:
            run = ctx.store.update_run(n, transient_errors=run.transient_errors + 1)
            if run.transient_errors >= 2 and ctx.config.executor_fallback_model and run.fallback_model is None:
                ctx.store.update_run(n, fallback_model=ctx.config.executor_fallback_model)
                _append(kwargs["log_path"], f"Executor fallback model: {ctx.config.executor_fallback_model}")
        budget = ctx.config.transient_max_wait_minutes * 60 - ctx.store.get_run(n).executor_waited_seconds
        if budget <= 0:
            if reviewer and kind == "limit":
                return replace(result, exit_code=result.exit_code or 1)
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Executor unavailable (transient errors)",
                 result.last_message or result.log_tail)
            raise ExecutorStopped("retries-exceeded")
        _executor_wait(n, ctx, min(120 * 2 ** min(errors, 3), budget))
        errors += 1


def _lane_notes(ctx: RunContext, n: int, worktree: Path) -> tuple[str | None, str]:
    lane = ctx.store.get_run(n).lane
    settings = ctx.config.lanes.get(lane, {}) if lane != "main" else {}
    agent = settings.get("agent")
    if not isinstance(agent, str) or not re.fullmatch(r"fa-[a-z0-9-]+", agent):
        return None, ""
    path = worktree / ".claude/agents" / f"{agent}.md"
    if not path.is_file():
        return None, ""
    body = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"\A---\s*\n.*?\n---[^\S\n]*(?:\n|$)", "", body, count=1, flags=re.DOTALL)
    memory = worktree / ".claude/agent-memory" / agent / "MEMORY.md"
    if memory.is_file():
        with memory.open(encoding="utf-8", errors="replace") as stream:
            body += "\n\nMemory:\n" + "".join(islice(stream, 200))
    return agent, "Lane notes:\n" + body.strip() + "\n\n"


def _lane_skill_names(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> list[str]:
    lane = ctx.store.get_run(n).lane
    settings = ctx.config.lanes.get(lane, {}) if lane != "main" else {}
    skills = settings.get("skills", [])
    frontmatter = re.compile(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", re.DOTALL | re.MULTILINE)
    if not skills and agent is not None:
        definition = (worktree / ".claude/agents" / f"{agent}.md").read_text(encoding="utf-8", errors="replace")
        match = frontmatter.match(definition)
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        skills = metadata.get("skills", []) if isinstance(metadata, dict) else []
    return [name for name in skills if isinstance(name, str)] if isinstance(skills, list) else []


def _inject_lane_skills(ctx: RunContext, n: int, git: GitRepo) -> None:
    agent, _ = _lane_notes(ctx, n, git.root)
    names = _lane_skill_names(ctx, n, git.root, agent)
    git.local_skill_paths = (
        set(copy_lane_skills(ctx.repo_root, git.root, names, SkillManager(ctx.config))) if names else set()
    )


def _lane_skills(ctx: RunContext, n: int, worktree: Path, agent: str | None) -> tuple[str, list[str]]:
    """List installed skills so Codex can read them when relevant."""
    skills = _lane_skill_names(ctx, n, worktree, agent)
    frontmatter = re.compile(r"\A---[^\S\n]*\n(.*?)^---[^\S\n]*(?:\n|$)", re.DOTALL | re.MULTILINE)
    sections, missing = [], []
    for name in skills:
        path = worktree / ".claude/skills" / name / "SKILL.md"
        if not path.resolve().is_relative_to(worktree.resolve()) or not path.is_file():
            missing.append(name)
            continue
        match = frontmatter.match(path.read_text(encoding="utf-8", errors="replace"))
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        description = metadata.get("description", "") if isinstance(metadata, dict) else ""
        description = " ".join(description.split()) if isinstance(description, str) else ""
        sections.append(f"- {name}: {description} (.claude/skills/{name}/SKILL.md)")
    return ("Lane skills (read the file when relevant and follow it):\n" + "\n".join(sections) + "\n\n"
            if sections else ""), missing


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


def _review(n: int, ctx: RunContext, git: GitRepo, parsed: ParsedIssue, base_ref: str,
            *, round_number: int = 1, pr: int | None = None) -> ReviewResult:
    _phase(n, ctx, "reviewing")
    reviewer = resolved_reviewer(ctx.config)
    options = {"review_level": ctx.config.review_level} if reviewer == "claude" else {}
    if reviewer == "claude" and pr is not None:
        options["review_pr"] = pr
    rebuttals = _read_rebuttals(git.root)
    edited = False
    with ExitStack() as scope:
        if reviewer == "claude" and ctx.config.review_scope == "claude-local":
            approval = ctx.store.load_approval(n)
            if approval is None:
                raise ValueError("Claude review requires an approved snapshot")
            scope.enter_context(_claude_review_scope(git, approval.body, ctx.config.acceptance_dir, rebuttals))
        before = git.review_state()
        try:
            result = _available_executor(
                n, ctx, ctx.executor_factory(reviewer), worktree=git.root,
                reviewer=True,
                prompt="" if reviewer == "claude" else build_review_prompt(
                    parsed, git.staged_diff(base_ref), ctx.config.acceptance_dir, rebuttals=rebuttals,
                ),
                model=ctx.config.reviewer_model, timeout_s=_remaining(n, ctx),
                log_path=ctx.store.log_path(n), **options,
            )
            message = result.full_message if result.full_message is not None else result.last_message
            raw = message
            if reviewer == "claude":
                try:
                    event = json.loads(message)
                except ValueError:
                    event = None
                if isinstance(event, dict) and isinstance(event.get("result"), str):
                    raw = event["result"]
            path = ctx.store.root / "runs" / f"issue-{n}" / f"review-{round_number}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
        finally:
            if git.review_state() != before:
                git.restore_review_state(before)
                edited = True
                _append(ctx.store.log_path(n), "Reviewer changed repository state; restored HEAD, index and working tree.")
    if result.timed_out or result.exit_code != 0:
        review = ReviewResult("UNUSABLE", "", (
            f"Reviewer exit_code={result.exit_code}, timed_out={result.timed_out}",
        ))
    else:
        parser = parse_code_review if reviewer == "claude" else parse_review
        review = parser(message)
    if reviewer == "claude":
        review = ReviewResult(review.verdict, review.findings, (
            f"reviewer: /code-review {ctx.config.review_level}", *review.notes,
        ))
    if edited:
        review = ReviewResult(review.verdict, review.findings, (*review.notes, "Reviewer changes discarded; staged index restored."))
    _append(ctx.store.log_path(n), f"Review: VERDICT: {review.verdict}\n{review.findings}")
    _progress(n, ctx)
    return replace(review, raw=raw, timed_out=result.timed_out)


def _read_rebuttals(worktree: Path) -> str:
    path = worktree / ".foragent/REBUTTAL.md"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _next_review_round(n: int, ctx: RunContext) -> int:
    rounds = [int(path.stem.removeprefix("review-"))
              for path in (ctx.store.root / "runs" / f"issue-{n}").glob("review-*.txt")
              if path.stem.removeprefix("review-").isdigit()]
    return max(rounds, default=0) + 1


def _normalized_findings(review: ReviewResult) -> set[str]:
    # Unstructured prose is one finding: repeated preambles alone are not a dispute.
    points = [review.raw] if review.verdict == "UNUSABLE" else review.findings.splitlines()
    return {" ".join(re.sub(r"^\s*[-*]\s+", "", point).split()).casefold()
            for point in points if point.strip()}


def _batch_pr(parent: int, ctx: RunContext) -> None:
    children = ctx.tracker.sub_issues(parent)
    if not children or any(child.state != "closed" for child in children):
        return
    branch = f"foragent/batch-{parent}"
    if any(pr.head == branch for pr in ctx.tracker.list_prs("merged", branch)):
        return
    prs = {pr.head: pr for pr in ctx.tracker.list_prs("merged", "foragent/issue-") if pr.base == branch}
    lines = [f"Closes #{parent}", "", "## Children", ""]
    for child in sorted(children, key=lambda child: child.number):
        pr = prs.get(f"foragent/issue-{child.number}")
        lines.append(f"- #{child.number}: {child.title}" + (f" (PR #{pr.number})" if pr else ""))
    title = f"Batch: {ctx.tracker.get_issue(parent).title}"
    body = "\n".join(lines)
    if (pr := ctx.tracker.find_pr(branch)) is not None:
        ctx.tracker.update_pr(pr, base=ctx.config.base_branch, title=title, body=body)
        board.sync_status(ctx, parent, "PR open")
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
    board.sync_status(ctx, parent, "PR open")
    ctx.tracker.comment(parent, f"PR #{pr} is waiting for your review\n\nhttps://github.com/{ctx.repo}/pull/{pr}")


def _acceptance_matches(
    n: int, ctx: RunContext, git: GitRepo, approval: Approval, ref: str = "HEAD",
) -> bool:
    blobs = git.blob_shas(ref, approval.acceptance_blobs)
    mismatches = [path for path, sha in approval.acceptance_blobs.items() if blobs.get(path) != sha]
    if mismatches:
        invalidate_approval(
            ctx, n, "Approval invalidated: worktree acceptance files changed. Re-approve to run it.",
            "\n".join(f"Acceptance file changed: `{path}`." for path in mismatches),
        )
    return not mismatches


def _execute(n: int, ctx: RunContext, worktree: Path) -> str:
    approval = ctx.store.load_approval(n)
    if approval is None:
        ctx.store.update_run(n, detail="No approved snapshot is available.")
        ctx.tracker.remove_labels(n, [RUNNING])
        return "no-approval"

    branch = f"foragent/issue-{n}"
    base_ref = f"origin/{ctx.config.base_branch}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    target_branch = ctx.config.base_branch
    ctx.git.remove_worktree(worktree)
    ctx.git.fetch("origin", ctx.config.base_branch)
    if not _acceptance_matches(n, ctx, ctx.git, approval, base_ref):
        return "hash-mismatch"
    if ctx.config.review == "batch" and batch_branch:
        ctx.git.ensure_batch_branch(batch_branch, base_ref)
        target_branch = batch_branch
    start_ref = f"origin/{target_branch}"
    ctx.git.add_worktree(worktree, branch, start_ref)
    git = ctx.git.at(worktree)
    _inject_lane_skills(ctx, n, git)
    if not _acceptance_matches(n, ctx, git, approval):
        return "hash-mismatch"
    _progress(n, ctx)

    parsed = parse_body(approval.body)
    if not has_machine_check(parsed):
        flag(ctx, n, Reason.NO_MACHINE_CHECK, "Done when has no machine check.")
        return "no-machine-check"
    return _implement_and_publish(n, ctx, git, target_branch=target_branch)


def _implement_and_publish(
    n: int, ctx: RunContext, git: GitRepo, *, target_branch: str, failure: str | CIFailure = "",
) -> str:
    """Run the common implementation/check/review/publish loop in an existing worktree."""
    approval = ctx.store.load_approval(n)
    parsed = parse_body(approval.body)
    worktree = git.root
    branch = f"foragent/issue-{n}"
    base_ref = f"origin/{ctx.config.base_branch}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    start_ref = f"origin/{target_branch}"
    executor = ctx.executor_factory(ctx.config.executor)
    native_claude = ctx.config.executor == "claude"
    options = {"settings_path": _write_claude_settings(n, ctx, worktree)} if native_claude else {}
    log = ctx.store.log_path(n)
    reviews: list[ReviewResult] = []
    review_failures = 0
    review_hold = False
    review_hold_reason = Reason.RISK_HIGH
    reviewer_text: str | None = None
    rebuttals: list[str] = []
    previous_rebutted_findings: set[str] = set()
    checks: list[CheckResult] = []
    paths: list[str] = []
    missing_skills: list[str] = []
    conflict_attempts = 0
    rebuilt = False
    rebuild_lease: str | None = None
    size = issue_size(ctx.tracker.get_issue(n).labels)
    baseline_test_command = ctx.config.test_command
    if baseline_test_command is None:
        baseline_test_command = next(iter(detect_test_commands(worktree)), None)
    baseline = _capture_acceptance_baseline(n, ctx, baseline_test_command, start_ref)

    def implement(
        failure: str | CIFailure, *, merge_conflicts: list[str] | None = None, reviewer_text: str | None = None,
    ) -> None:
        _phase(n, ctx, "resolving-conflict" if merge_conflicts is not None else "implementing")
        remaining = _remaining(n, ctx)
        if remaining <= 0:
            raise ExecutorStopped(_timeout(n, ctx))
        run = ctx.store.get_run(n)
        tier = attempt_tier(size, run.attempts)
        ctx.store.update_run(n, tier=tier, attempt_tiers={**run.attempt_tiers, str(run.attempts): tier})
        settings = ctx.config.tiers[tier]
        lane = ctx.config.lanes.get(run.lane, {})
        model = ctx.config.executor_model
        if native_claude:
            model = lane.get("model") or settings["claude_model"] or model
        else:
            options["effort"] = settings["codex_effort"]
        agent, notes = _lane_notes(ctx, n, worktree)
        if native_claude and agent is not None:
            options["agent"] = agent
        if not native_claude:
            skill_notes, missing = _lane_skills(ctx, n, worktree, agent)
            notes += skill_notes
            missing_skills.extend(name for name in missing if name not in missing_skills)
        hint_path = ctx.store.root / "hints" / f"{n}.txt"
        hint = hint_path.read_text(encoding="utf-8") if hint_path.is_file() else None
        failure_context = str(failure) or run.resume_reason
        test_command = ctx.config.test_command
        if test_command is None:
            test_command = next(iter(detect_test_commands(worktree)), None)
        prompt = build_prompt(
            n, parsed, ctx.config.acceptance_dir, failure_context, ctx.config.language,
            native_goal=native_claude or ctx.config.codex_goal, acceptance_locked=native_claude,
            merge_conflicts=merge_conflicts,
            reviewer_text=reviewer_text,
            orchestrator_hint=hint,
            test_command=test_command,
            acceptance_baseline=baseline.node_ids if baseline else (),
            failure_limit=None if isinstance(failure, CIFailure) else 4000,
        )
        result = _available_executor(
            n, ctx, executor, worktree=worktree,
            prompt=prompt if native_claude else notes + prompt,
            session_body=approval.body,
            resume_prompt=build_resume_prompt(
                n, run.session_body, approval.body, failure_context,
                reviewer_text=reviewer_text, orchestrator_hint=hint, merge_conflicts=merge_conflicts,
            ),
            model=model, timeout_s=remaining, log_path=log, **options,
        )
        # A prepared prompt has not been delivered yet. Keep its hint through
        # availability waits, launch failures and interruptions; acknowledge it
        # only once the implementer returns an ordinary execution result.
        if hint is not None and classify_result(result) == "normal":
            hint_path.unlink()
        rebuttal = _read_rebuttals(worktree)
        if rebuttal.strip() and rebuttal not in rebuttals:
            rebuttals.append(rebuttal)
        if result.timed_out or _remaining(n, ctx) <= 0:
            raise ExecutorStopped(_timeout(n, ctx))
        question = worktree / ".foragent/QUESTION.md"
        if question.exists():
            with question.open(encoding="utf-8", errors="replace") as stream:
                details = stream.read(6000)
            flag(ctx, n, Reason.SPEC_QUESTION, "The executor needs a product decision.", details)
            raise ExecutorStopped("spec-question")

    def recreate_worktree() -> None:
        artifacts = {name: path.read_bytes() for name in ("TRY.md", "REBUTTAL.md")
                     if (path := worktree / ".foragent" / name).is_file()}
        ctx.git.remove_worktree(worktree)
        ctx.git.add_worktree(worktree, branch, start_ref)
        _inject_lane_skills(ctx, n, git)
        for name, content in artifacts.items():
            path = worktree / ".foragent" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def prepare_target() -> bool:
        nonlocal start_ref, target_branch
        if not batch_branch:
            return True
        paths = git.changed_paths(start_ref)
        mode = review_mode_for(paths, ctx.config)
        destination = batch_branch if mode == "batch" else ctx.config.base_branch if mode == "fast" else target_branch
        if destination == target_branch:
            return True
        # Path overrides can select batch after implementation or generated
        # files, or return to fast after a retry removes those paths.
        if destination == batch_branch:
            ctx.git.ensure_batch_branch(batch_branch, base_ref)
        with tempfile.NamedTemporaryFile() as patch:
            git.write_staged_patch(start_ref, Path(patch.name))
            start_ref = f"origin/{destination}"
            target_branch = destination
            recreate_worktree()
            if not _acceptance_matches(n, ctx, git, approval):
                return False
            try:
                git.apply_staged_patch(Path(patch.name))
            except GitError as exc:
                raise PatchReplayError(str(exc)) from exc
        return True

    def recover_replay(exc: PatchReplayError) -> str:
        nonlocal paths
        # A three-way failure can leave unmerged index entries. Start the
        # next implementation attempt from the destination's clean tree.
        recreate_worktree()
        paths = []
        return f"Could not apply child changes to {target_branch}: {exc}"

    def rebuild(exc: ConflictBudgetExhausted) -> str:
        nonlocal start_ref, rebuilt, rebuild_lease, paths
        rebuilt = True
        _append(log, f"Rebuilding issue branch once from latest {target_branch}.\n{exc}")
        ctx.git.fetch("origin", target_branch)
        start_ref = f"origin/{target_branch}"
        if not _acceptance_matches(n, ctx, ctx.git, approval, start_ref):
            raise ExecutorStopped("hash-mismatch")
        # Bind every rewritten push to the remote head observed before rebuilding.
        # An empty lease means the branch must still be absent.
        rebuild_lease = git.remote_branch_sha(branch) or ""
        _append(log, f"Rebuild target: {start_ref} ({git.ref_sha(start_ref)})")
        recreate_worktree()
        paths = []
        run = ctx.store.get_run(n)
        ctx.store.update_run(n, attempts=run.attempts + 1)
        return (
            f"The earlier attempt could not be merged onto the moved base ({target_branch}). "
            "The issue branch and worktree have been rebuilt from the latest target branch. "
            "Implement the original instructions again, preserving the features now on the base."
        )

    def push_branch() -> None:
        nonlocal rebuild_lease
        if rebuilt:
            git.push(branch, lease=rebuild_lease)
            rebuild_lease = git.head_sha()
        else:
            git.push(branch)

    def block_rebuild(details: str) -> str:
        _append(log, f"Rebuilt attempt failed:\n{details}")
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Rebuilt implementation failed; no attempts remain.", details)
        return "retries-exceeded"

    def preserve_failed_implementation() -> None:
        # Preserve the failed implementation, rather than pushing an unchanged base.
        if paths and git.has_staged_changes():
            title = ctx.tracker.get_issue(n).title
            git.commit(f"{title} (#{n})")
        push_branch()

    def verify() -> str:
        nonlocal checks, paths
        _phase(n, ctx, "checking")
        git.stage_changes()
        if not prepare_target():
            raise ExecutorStopped("hash-mismatch")
        paths = git.changed_paths(start_ref)
        _progress(n, ctx)
        checks = []
        if not paths:
            return "The session made no changes."
        staged_tree = git.staged_tree_sha()
        for verification in range(2):
            checks = []
            for command in parsed.done_when_commands:
                checks.append(_check(command, worktree, log, 900, progress=lambda: _progress(n, ctx)))
                _progress(n, ctx)
            if baseline and baseline.node_ids:
                command = f"{baseline.command} {shlex.join(baseline.node_ids)}"
                result = _check(command, worktree, log, 900, progress=lambda: _progress(n, ctx))
                checks.append(result)
                _append(log, f"Acceptance baseline verification count={len(baseline.node_ids)}, "
                        f"exit_code={result.exit_code}, timed_out={result.timed_out}")
                _progress(n, ctx)
            test_command = ctx.config.test_command
            if test_command is None:
                test_command = next(iter(detect_test_commands(worktree)), None)
            unit_command = _pytest_command(test_command) if test_command else None
            unit_tests = _related_unit_tests(worktree, paths, ctx.config.acceptance_dir)
            if unit_tests and unit_command:
                limit = max(900, _remaining(n, ctx))
                result = _check(f"{unit_command} {shlex.join(unit_tests)}", worktree, log, limit,
                                progress=lambda: _progress(n, ctx))
                checks.append(result)
                _progress(n, ctx)
                if result.timed_out:
                    detail = f"Related unit tests timed out after {limit} s ({len(unit_tests)} selected tests)."
                    git.stage_changes()
                    preserve_failed_implementation()
                    flag(ctx, n, Reason.TIMEOUT, detail)
                    raise ExecutorStopped("timeout", detail)
            elif unit_tests:
                _append(log, "Related unit tests skipped: no supported pytest command is known")
            git.stage_changes()
            if not prepare_target():
                raise ExecutorStopped("hash-mismatch")
            paths = git.changed_paths(start_ref)
            checked_tree = git.staged_tree_sha()
            if checked_tree != staged_tree:
                if verification == 1:
                    return "Done when commands modify tracked files"
                _append(log, "Done when changed staged contents; checking the final contents again.")
                staged_tree = checked_tree
                continue
            failures = [check for check in checks if check.exit_code != 0 or check.timed_out]
            if failures:
                check = failures[-1]
                return f"Command: {check.command}\n{check.output}" + ("\nCheck timed out." if check.timed_out else "")
            return "" if paths else "The session made no changes."
        raise AssertionError("unreachable verification state")

    def render_pr_body(try_text: str | None = None) -> str:
        lane = ctx.store.get_run(n).lane
        globs = ctx.config.lanes.get(lane, {}).get("paths", []) if lane != "main" else None
        inside = set(touched(paths, globs)) if globs is not None else set(paths)
        outside = [path for path in paths if path not in inside]
        return _pr_body(
            n, parsed, checks, ctx.store.get_run(n).attempts, reviews, try_text, outside_lane=outside,
            fallback_model=ctx.store.get_run(n).fallback_model,
            attempt_tiers=ctx.store.get_run(n).attempt_tiers,
            missing_skills=missing_skills,
            rebuttals="\n\n".join(rebuttals),
            review_scope=ctx.config.review_scope, acceptance_dir=ctx.config.acceptance_dir,
        )

    def open_or_update_pr(title: str, body: str) -> int:
        pr = ctx.tracker.find_pr(branch)
        if pr is None:
            pr = ctx.tracker.create_pr(head=branch, base=target_branch, title=title, body=body)
        else:
            ctx.tracker.update_pr(pr, base=target_branch, title=title, body=body)
        board.sync_status(ctx, n, "PR open")
        return pr

    def publish() -> str:
        with _merge_lock(n, ctx):
            return publish_locked()

    def publish_locked() -> str:
        # Only metadata is read live; instructions and checks always use the snapshot.
        issue = ctx.tracker.get_issue(n)
        title = f"{issue.title} (#{n})"
        if git.has_staged_changes():
            git.commit(title)
        _progress(n, ctx)
        verified_head_sha = git.head_sha()
        base_merges = 0

        def resolve_conflicts(conflicts: list[str]) -> None:
            nonlocal conflict_attempts
            pending_merge = True
            check_failure = f"Merge conflict with {target_branch}: {', '.join(conflicts)}"
            while True:
                if rebuilt:
                    raise ExecutorStopped(block_rebuild(check_failure))
                if conflict_attempts >= ctx.config.conflict_retries:
                    raise ConflictBudgetExhausted(check_failure)
                conflict_attempts += 1
                _append(log, f"Conflict attempt {conflict_attempts}/{ctx.config.conflict_retries}:\n{check_failure}")
                implement(check_failure, merge_conflicts=conflicts)
                # Retain the merge's paths even if the executor stages them.
                markers = git.conflict_marker_paths(conflicts)
                if markers:
                    check_failure = f"Conflict markers remain in: {', '.join(markers)}"
                    continue
                git.stage_changes()
                if pending_merge:
                    git.continue_merge()
                    pending_merge = False
                elif git.has_staged_changes():
                    git.commit(title)
                check_failure = verify()
                markers = git.conflict_marker_paths(conflicts)
                if markers:
                    check_failure = f"Conflict markers remain in: {', '.join(markers)}"
                if not check_failure:
                    return

        def refresh_base() -> str | None:
            nonlocal verified_head_sha, base_merges
            ctx.git.fetch("origin", target_branch)
            target_ref = f"origin/{target_branch}"
            target_sha = git.ref_sha(target_ref)
            if git.is_ancestor(target_sha, verified_head_sha):
                return None
            if base_merges == 3:
                flag(ctx, n, Reason.TIMEOUT, "base kept moving")
                raise ExecutorStopped("timeout")
            try:
                git.merge_base(target_ref)
            except GitError:
                conflicts = git.conflict_paths()
                if not conflicts:
                    raise
                resolve_conflicts(conflicts)
            else:
                check_failure = verify()
                if check_failure:
                    raise AttemptFailure(check_failure)
            base_merges += 1
            if _remaining(n, ctx) <= 0:
                raise ExecutorStopped(_timeout(n, ctx))
            if git.has_staged_changes():
                git.commit(title)
            verified_head_sha = git.head_sha()
            return target_sha

        target_sha = refresh_base() if not review_hold else None
        if not review_hold and _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        push_branch()
        _progress(n, ctx)
        _append(log, f"Pushed verified HEAD: {verified_head_sha}")
        try_text = None
        try_path = worktree / ".foragent/TRY.md"
        if not review_hold and try_path.is_file():
            try_text = try_path.read_bytes().decode("utf-8", errors="replace")
            saved_try = ctx.store.root / "try" / f"{n}.md"
            saved_try.parent.mkdir(parents=True, exist_ok=True)
            saved_try.write_text(try_text, encoding="utf-8")

        def pr_body() -> str:
            return render_pr_body(try_text)

        pr = open_or_update_pr(title, pr_body())
        if target_sha is not None:
            ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
        _progress(n, ctx)
        pr_ready_at = ctx.now().timestamp()
        link = f"https://github.com/{ctx.repo}/pull/{pr}"
        if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
            return _hold_changed_head(n, ctx, link)
        if review_hold:
            summary = (
                "Reviewer repeated a disputed finding after implementer rebuttals"
                if review_hold_reason == Reason.REVIEW_DISPUTE
                else f"Reviewer still found defects after {review_failures} rounds"
            )
            details = f"{failure}\n\n{link}"
            flag(ctx, n, review_hold_reason, summary, details)
            return "held"
        decision = merge_decision(
            changed_paths=ctx.tracker.pr_changed_files(pr),
            issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
        )
        if decision.kind == "hold":
            flag(ctx, n, decision.reason, decision.summary, link)
            return "held"
        if decision.kind == "hold_mode" and (decision.mode == "careful" or not batch_branch):
            ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
            ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
            ctx.tracker.remove_labels(n, [RUNNING])
            return "pr-open"

        # A bounded iteration count also works with a frozen test clock.
        for _ in range(4):
            _phase(n, ctx, "ci")
            has_workflows = any(
                path.is_file()
                for pattern in ("*.yml", "*.yaml")
                for path in (worktree / ".github/workflows").glob(pattern)
            )
            deadline = ctx.now().timestamp() + 20 * 60
            for poll in range(41):
                status = ctx.tracker.pr_checks(pr)
                _progress(n, ctx)
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                # Actions checks can register after each push, including base merges.
                if status == "none" and has_workflows and ctx.now().timestamp() - pr_ready_at < 180:
                    status = "pending"
                if status in {"pass", "none"}:
                    break
                if status == "fail":
                    if rebuilt:
                        raise ExecutorStopped(block_rebuild(f"CI failed:\n{ctx.tracker.pr_failed_logs(pr)}"))
                    _ci_failure(n, ctx, pr, verified_head_sha, target_branch)
                remaining = deadline - ctx.now().timestamp()
                if remaining <= 0 or poll == 40:
                    flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                    return "timeout"
                ctx.sleep(min(30, remaining))

            target_sha = refresh_base()
            target_ref = f"origin/{target_branch}"
            if target_sha is not None:
                push_branch()
                _progress(n, ctx)
                _append(log, f"Pushed verified HEAD: {verified_head_sha}")
                ctx.tracker.update_pr(pr, base=target_branch, title=title, body=pr_body())
                ctx.tracker.comment(pr, f"base merged before merge: {target_sha}")
                pr_ready_at = ctx.now().timestamp()
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                decision = merge_decision(
                    changed_paths=git.changed_paths(target_ref),
                    issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config,
                )
                if decision.kind == "hold":
                    flag(ctx, n, decision.reason, decision.summary, link)
                    return "held"
                if decision.kind == "hold_mode" and (decision.mode == "careful" or not batch_branch):
                    ctx.store.update_run(n, detail=f"PR #{pr} is waiting for your review.")
                    ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
                    ctx.tracker.remove_labels(n, [RUNNING])
                    return "pr-open"
                continue
            _phase(n, ctx, "merging")
            try:
                ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                raise
            if decision.kind == "merge":
                after_merge(ctx, pr, target_branch)
            _progress(n, ctx)
            try:
                if target_branch == batch_branch:
                    ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr})")
                else:
                    ctx.tracker.close_issue(n)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                if ctx.tracker.get_issue(n).state != "closed":
                    raise
            ctx.tracker.remove_labels(n, [READY, RUNNING])
            board.sync_status(ctx, n, "Done")
            if target_branch == batch_branch and parent is not None:
                try:
                    _batch_pr(parent, ctx)
                except TrackerError as exc:
                    if exc.is_rate_limit:
                        raise
                    _append(log, f"Batch PR finalization deferred to tick: {exc}")
            _progress(n, ctx)
            return "merged"
        raise AssertionError("unreachable refresh state")

    while True:
        implement(failure, reviewer_text=reviewer_text)
        reviewer_text = None
        run = ctx.store.get_run(n)

        try:
            failure = verify()
        except PatchReplayError as exc:
            failure = recover_replay(exc)
        if not failure and ctx.config.reviewer != "none":
            if _remaining(n, ctx) <= 0:
                return _timeout(n, ctx)
            pr = None
            if ctx.config.review_scope == "pr-body":
                title = f"{ctx.tracker.get_issue(n).title} (#{n})"
                if git.has_staged_changes():
                    git.commit(title)
                push_branch()
                pr = open_or_update_pr(title, render_pr_body())
                _progress(n, ctx)
            review = _review(n, ctx, git, parsed, base_ref, round_number=_next_review_round(n, ctx), pr=pr)
            if review.verdict == "UNUSABLE":
                review = replace(review, findings=review.raw)
            reviews.append(review)
            if pr is not None:
                ctx.tracker.comment(pr, "\n".join([
                    f"Round {len(reviews)}: VERDICT: {review.verdict}", review.findings, *review.notes,
                ]))
            if review.verdict != "PASS":
                review_failures += 1
                if review.verdict == "UNUSABLE":
                    reviewer_text = review.raw
                    failure = "A reviewer said (verbatim):\n" + review.raw
                else:
                    failure = review.findings or "Reviewer found defects (no findings provided)."
                findings = _normalized_findings(review) if _read_rebuttals(worktree).strip() else set()
                if findings & previous_rebutted_findings:
                    review_hold = True
                    review_hold_reason = Reason.REVIEW_DISPUTE
                previous_rebutted_findings = findings
                if review_failures > ctx.config.review_max:
                    review_hold = True
        if rebuilt and failure:
            return block_rebuild(str(failure))
        if not failure or review_hold:
            try:
                return publish()
            except ConflictBudgetExhausted as exc:
                failure = rebuild(exc)
                continue
            except CIFailure as exc:
                if rebuilt:
                    return block_rebuild(str(exc))
                failure = exc
                continue
            except (AttemptFailure, PatchReplayError) as exc:
                failure = recover_replay(exc) if isinstance(exc, PatchReplayError) else str(exc)
                run = ctx.store.get_run(n)
        if rebuilt:
            return block_rebuild(str(failure))
        _append(log, f"Attempt {run.attempts} failed:\n{failure}")
        ctx.store.update_run(n, resume_reason=str(failure))
        if run.attempts < ctx.config.max_retries + 1:
            ctx.store.update_run(n, attempts=run.attempts + 1)
            continue
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Checks or review failed after all attempts.", failure)
        preserve_failed_implementation()
        return "retries-exceeded"


def _review_only(n: int, ctx: RunContext, worktree: Path, verdict_head: str | None) -> str:
    """Verify one immutable PR head, then apply the ordinary merge policy."""
    approval = ctx.store.load_approval(n)
    if approval is None:
        invalidate_approval(ctx, n, "Review requires a current approved snapshot. Re-approve to run it.")
        return "no-approval"
    pr = next((pr for pr in ctx.tracker.list_prs("open", f"foragent/issue-{n}")
               if pr.head == f"foragent/issue-{n}"), None)
    if pr is None:
        flag(ctx, n, Reason.RISK_HIGH, "No open Foragent PR to review; retry implementation.")
        return "no-pr"
    head = ctx.tracker.pr_head_sha(pr.number)
    link = f"https://github.com/{ctx.repo}/pull/{pr.number}"
    parent = ctx.tracker.parent_of(n) if "batch" in {ctx.config.review, *ctx.config.review_paths.values()} else None
    batch_branch = f"foragent/batch-{parent}" if parent is not None else None
    if pr.base not in {ctx.config.base_branch, batch_branch}:
        flag(ctx, n, Reason.RISK_HIGH, "PR targets an unexpected base; retry implementation.", link)
        return "base-changed"
    if verdict_head is not None and head != verdict_head:
        return _hold_changed_head(n, ctx, link)

    def approved() -> bool:
        issue = ctx.tracker.get_issue(n)
        if APPROVED not in issue.labels or body_hash(issue.body) != approval.body_sha:
            invalidate_approval(ctx, n, "Approval invalidated: issue or approval changed. Re-approve to run it.")
            return False
        return True

    if not approved():
        return "hash-mismatch"
    parsed = parse_body(approval.body)
    if not has_machine_check(parsed):
        flag(ctx, n, Reason.NO_MACHINE_CHECK, "Done when has no machine check.")
        return "no-machine-check"
    ctx.git.fetch("origin", ctx.config.base_branch)
    base_ref = f"origin/{pr.base}"
    if pr.base != ctx.config.base_branch:
        ctx.git.fetch("origin", pr.base)
    base_sha = ctx.git.ref_sha(base_ref)
    if not _acceptance_matches(n, ctx, ctx.git, approval, f"origin/{ctx.config.base_branch}"):
        return "hash-mismatch"
    ctx.git.fetch("origin", pr.head)
    if ctx.git.ref_sha(f"origin/{pr.head}") != head:
        return _hold_changed_head(n, ctx, link)
    ctx.git.add_detached_worktree(worktree, head)
    git = ctx.git.at(worktree)
    if not _acceptance_matches(n, ctx, git, approval):
        return "hash-mismatch"
    if not git.is_ancestor(base_sha, head):
        flag(ctx, n, Reason.RISK_HIGH, "PR base changed; retry implementation on the current base.", link)
        return "base-changed"

    log = ctx.store.log_path(n)
    for command in parsed.done_when_commands:
        if _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        check = _check(command, worktree, log, min(900, _remaining(n, ctx)), progress=lambda: _progress(n, ctx))
        _progress(n, ctx)
        if check.timed_out or _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if check.exit_code != 0:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when failed on the PR head.",
                 f"Command: {command}\n{check.output}\n{link}")
            return "checks-failed"
    git.stage_changes()
    if git.head_sha() != head or git.has_staged_changes():
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Done when changed the verified PR contents.", link)
        return "checks-failed"

    if verdict_head is None and ctx.config.reviewer != "none":
        if _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if ctx.config.review_scope == "pr-body" and "## Review scope" not in pr.body:
            ctx.tracker.update_pr(pr.number, base=pr.base, title=pr.title,
                                  body=pr.body + "\n\n## Review scope\n\n" + review_scope_rules(ctx.config.acceptance_dir))
        review = _review(n, ctx, git, parsed, base_ref, round_number=_next_review_round(n, ctx),
                         pr=pr.number if ctx.config.review_scope == "pr-body" else None)
        ctx.tracker.comment(pr.number, f"Re-run review: VERDICT: {review.verdict}\n{review.raw}")
        if review.timed_out or _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        if review.verdict != "PASS":
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Re-run review {review.verdict}.", review.raw or review.findings)
            return "review-failed"

    with _merge_lock(n, ctx):
        # A pass applies only to this head and these instructions. Neither the
        # reviewer nor an orchestrator verdict can waive the remaining checks.
        def unchanged() -> str | None:
            if not approved():
                return "hash-mismatch"
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            current = next((item for item in ctx.tracker.list_prs("open", pr.head)
                            if item.number == pr.number), None)
            ctx.git.fetch("origin", pr.base)
            if current is None or current.base != pr.base or ctx.git.ref_sha(base_ref) != base_sha:
                flag(ctx, n, Reason.RISK_HIGH, "PR base changed after verification; retry implementation.", link)
                return "base-changed"
            if not _acceptance_matches(n, ctx, git, approval):
                return "hash-mismatch"
            return None

        if stopped := unchanged():
            return stopped
        paths = sorted(set(git.changed_paths(base_ref)) | set(ctx.tracker.pr_changed_files(pr.number)))

        def policy_hold() -> str | None:
            decision = merge_decision(changed_paths=paths, issue_labels=ctx.tracker.get_issue(n).labels, config=ctx.config)
            if decision.kind == "hold":
                flag(ctx, n, decision.reason, decision.summary, link)
                return "held"
            if decision.kind == "hold_mode" and (decision.mode == "careful" or pr.base != batch_branch):
                flag(ctx, n, Reason.RISK_HIGH, "PR is waiting for your review.", link)
                return "pr-open"
            return None

        if stopped := policy_hold():
            return stopped
        has_workflows = any(path.is_file() for pattern in ("*.yml", "*.yaml")
                            for path in (worktree / ".github/workflows").glob(pattern))
        started = ctx.now().timestamp()
        for poll in range(41):
            if _remaining(n, ctx) <= 0:
                return _timeout(n, ctx)
            status = ctx.tracker.pr_checks(pr.number)
            _progress(n, ctx)
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            if status == "none" and has_workflows and ctx.now().timestamp() - started < 180:
                status = "pending"
            if status in {"pass", "none"}:
                break
            if status == "fail":
                _ci_failure(n, ctx, pr.number, head, pr.base)
            if ctx.now().timestamp() - started >= 1200 or poll == 40:
                flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                return "timeout"
            ctx.sleep(min(30, _remaining(n, ctx)))
        if stopped := unchanged():
            return stopped
        if stopped := policy_hold():
            return stopped
        if _remaining(n, ctx) <= 0:
            return _timeout(n, ctx)
        try:
            ctx.tracker.merge_pr(pr.number, method="squash", expected_head_sha=head)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            if ctx.tracker.pr_head_sha(pr.number) != head:
                return _hold_changed_head(n, ctx, link)
            raise
        if review_mode_for(paths, ctx.config) == "fast":
            after_merge(ctx, pr.number, pr.base)
        try:
            if pr.base == batch_branch:
                ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr.number})")
            else:
                ctx.tracker.close_issue(n)
        except TrackerError as exc:
            if exc.is_rate_limit:
                raise
            if ctx.tracker.get_issue(n).state != "closed":
                raise
        ctx.tracker.remove_labels(n, [READY, RUNNING, BLOCKED])
        if pr.base == batch_branch and parent is not None:
            try:
                _batch_pr(parent, ctx)
            except TrackerError as exc:
                if exc.is_rate_limit:
                    raise
                _append(log, f"Batch PR finalization deferred to tick: {exc}")
        return "merged"


def review_only(
    n: int, ctx: RunContext, *, verdict_head: str | None = None, repair_ci: bool = True,
) -> str:
    """Review a PR, optionally repairing CI inline instead of returning its failure."""
    worktree = ctx.store.worktree_path(n)
    outcome = "error"
    detail = ""
    previous = ctx.store.get_run(n)
    started = False
    try:
        if previous is not None and previous.status == "running":
            raise TrackerError(f"issue #{n} already has a running execution")
        if until := ctx.store.github_wait_until(ctx.now()):
            ctx.store.start_run(n, branch=f"foragent/issue-{n}", worktree=worktree,
                                lane=previous.lane if previous is not None else "main")
            started = True
            outcome = orch_github.wait_run(ctx, n, until)
            return outcome
        issue = ctx.tracker.get_issue(n)
        if issue.state != "open" or issue.is_pull_request:
            raise ValueError(f"#{n} must be an open issue")
        if RUNNING in issue.labels:
            raise TrackerError(f"issue #{n} already has a running execution")
        ctx.store.start_run(n, branch=f"foragent/issue-{n}", worktree=worktree, pid=os.getpid(),
                            lane=issue_lane(issue.labels, ctx.config.lanes))
        started = True
        ctx.tracker.add_labels(n, [RUNNING])
        ctx.tracker.remove_labels(n, [BLOCKED, NEEDS_HUMAN, READY])
        ctx.git.remove_worktree(worktree)
        try:
            outcome = _review_only(n, ctx, worktree, verdict_head)
        except CIFailure as exc:
            if not repair_ci:
                # Let the caller schedule repair without changing the verdict's head.
                raise ExecutorStopped("ci-failed", str(exc)) from exc
            git = ctx.git.at(worktree)
            git.attach_branch(f"foragent/issue-{n}")
            _inject_lane_skills(ctx, n, git)
            outcome = _implement_and_publish(n, ctx, git, target_branch=exc.target_branch, failure=exc)
    except ExecutorStopped as exc:
        outcome = exc.outcome
        detail = str(exc)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        if not started:
            if not isinstance(exc, TrackerError) or not exc.is_rate_limit:
                raise
            ctx.store.start_run(n, branch=f"foragent/issue-{n}", worktree=worktree,
                                lane=previous.lane if previous is not None else "main")
            started = True
        _append(ctx.store.log_path(n), traceback.format_exc())
        if isinstance(exc, TrackerError) and wait_for_rate_limit(n, ctx, exc):
            outcome = "limit-wait"
        else:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Review runner failed: {' '.join(str(exc).split())}")
    except SystemExit as exc:
        outcome = "killed"
        detail = f"Review runner exited: {exc}"
        raise
    finally:
        if started:
            try:
                ctx.git.remove_worktree(worktree)
            finally:
                try:
                    # A rate-limit wait is discoverable locally; defer label repair.
                    if outcome != "limit-wait":
                        current = ctx.tracker.get_issue(n)
                        if current.state == "open" and not current.labels & {BLOCKED, READY}:
                            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Review did not finish; retry or rerun review.")
                        ctx.tracker.remove_labels(n, [RUNNING])
                except TrackerError as exc:
                    if not wait_for_rate_limit(n, ctx, exc):
                        raise
                    detail = str(exc) or type(exc).__name__
                    outcome = "limit-wait"
                finally:
                    try:
                        ctx.store.finish_run(n, outcome=outcome, detail=detail)
                    finally:
                        kick_tick(ctx.repo_root)
    return outcome


def run_issue(n: int, ctx: RunContext) -> str:
    outcome = "error"
    detail = ""
    worktree = ctx.store.worktree_path(n)
    log = ctx.store.log_path(n)
    try:
        run = ctx.store.update_run(n, pid=os.getpid())
        if until := ctx.store.github_wait_until(ctx.now()):
            outcome = orch_github.wait_run(ctx, n, until)
            return outcome
        board.sync_status(ctx, n, "Running")
        lane = ctx.config.lanes.get(run.lane, {}) if run.lane != "main" else {}
        ctx = replace(ctx, config=replace(
            ctx.config, executor=lane.get("executor") or ctx.config.executor,
            executor_model=lane.get("model") if lane.get("model") is not None else ctx.config.executor_model,
        ))
        try:
            outcome = _execute(n, ctx, worktree)
        finally:
            ctx.git.remove_worktree(worktree)
    except ExecutorStopped as exc:
        outcome = exc.outcome
        detail = str(exc)
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        _append(log, traceback.format_exc())
        if isinstance(exc, TrackerError) and wait_for_rate_limit(n, ctx, exc):
            outcome = "limit-wait"
        else:
            flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Runner failed: {' '.join(str(exc).split())}")
            outcome = "error"
    except SystemExit as exc:
        outcome = "killed"
        detail = f"Runner exited: {exc}"
        raise
    finally:
        # A tick may already have finished the record while terminating this runner.
        try:
            run = ctx.store.get_run(n)
            if run is not None and run.status != "finished":
                ctx.store.finish_run(n, outcome=run.outcome or outcome, detail=detail)
        finally:
            kick_tick(ctx.repo_root)
    return outcome
