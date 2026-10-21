"""Execute, independently check and review one approved snapshot."""

from collections.abc import Callable
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import islice
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

import yaml

from foragent.actions import flag, invalidate_approval
from foragent.config import Config, resolved_reviewer
from foragent.executors import ExecResult, Executor, classify_result, executor_for
from foragent.git import GitError, GitRepo, REVIEW_SCOPE_FILE
from foragent.issue import ParsedIssue, has_machine_check, parse_body
from foragent.kick import kick_tick
from foragent.labels import READY, RUNNING, attempt_tier, issue_size
from foragent.policy import merge_decision, review_mode_for, touched
from foragent.processes import run_logged
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


class PatchReplayError(GitError):
    """A child patch could not be replayed onto its selected destination."""


class AttemptFailure(Exception):
    """Fresh-base conflicts and checks share the implementation retry budget."""


class ExecutorStopped(Exception):
    def __init__(self, outcome: str):
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
    findings = [line for line in lines if re.match(r"^(?:[-*]\s+)?\S+:\d+\s+(?:—|-|--)\s+", line)]
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
) -> str:
    commands = "\n".join(f"```sh\n{command}\n```" for command in parsed.done_when_commands)
    acceptance_rule = "" if acceptance_locked else f"- Do not modify anything under {acceptance_dir}. Those tests are fixed.\n"
    prompt = (
        f"You are implementing GitHub issue #{n} in this repository.\n"
        f"{parsed.goal}\n"
        f"Out of scope (do not do these): {parsed.out_of_scope}\n"
        "The work is accepted only when these commands succeed (they will be run for you afterwards):\n"
        f"{commands}\n"
        "Rules:\n"
        f"{acceptance_rule}"
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
        prompt += "\nA previous attempt failed. Output of the failing check:\n" + failure[-4000:]
    if merge_conflicts is not None:
        prompt += (
            "\n\nResolve the conflict markers in the following files:\n"
            + "\n".join(f"- {path}" for path in merge_conflicts)
            + "\nPreserve both the features newly added on the base branch and the features for this issue. "
            "Do not commit. The runner will stage the resolution and create the merge commit."
        )
    return f"/goal {goal_condition(acceptance_dir)}\n\n{prompt}" if native_goal else prompt


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")


def _check(command: str, worktree: Path, log: Path, timeout_s: int) -> CheckResult:
    _append(log, f"\nDone when command:\n{command}")
    offset = log.stat().st_size
    code, timed_out, _ = run_logged(
        ["/bin/sh", "-c", command], cwd=worktree, timeout_s=timeout_s, log_path=log,
    )
    with log.open("rb") as stream:
        stream.seek(max(offset, log.stat().st_size - 16000))
        tail = stream.read().decode("utf-8", errors="replace")[-4000:]
    _append(log, f"\nCheck exit_code={code}, timed_out={timed_out}\nOutput tail:\n{tail}")
    return CheckResult(command, code, timed_out, tail)


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

    try:
        result = ctx.poll_hook(lambda: executor.run(**kwargs), poll)
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(offset, log.stat().st_size - 16000))
                result.log_tail = stream.read().decode("utf-8", errors="replace")[-4000:]
        return result
    finally:
        poll()
        _progress(n, ctx)


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


def _available_executor(n: int, ctx: RunContext, executor: Executor, *, reviewer: bool = False, **kwargs) -> ExecResult:
    errors = 0
    while True:
        run = ctx.store.get_run(n)
        if not reviewer and run.fallback_model is not None:
            kwargs["model"] = run.fallback_model
        kwargs["timeout_s"] = _remaining(n, ctx)
        result = _run_executor(n, ctx, executor, **kwargs)
        kind = classify_result(result)
        if kind == "normal":
            return result
        if kind == "transient" and _remaining(n, ctx) <= 0:
            return replace(result, timed_out=True)
        if kind == "limit" and not reviewer:
            ctx.store.pause_lane(run.lane, ctx.now() + timedelta(minutes=ctx.config.limit_wait_minutes))
            ctx.tracker.add_labels(n, [READY])
            ctx.tracker.remove_labels(n, [RUNNING])
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
            flag(ctx, n, Reason.RETRIES_EXCEEDED, "Executor unavailable (transient errors)")
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
    return replace(review, raw=raw)


def _read_rebuttals(worktree: Path) -> str:
    path = worktree / ".foragent/REBUTTAL.md"
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


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
        return
    pr = ctx.tracker.create_pr(head=branch, base=ctx.config.base_branch, title=title, body=body)
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
    executor = ctx.executor_factory(ctx.config.executor)
    native_claude = ctx.config.executor == "claude"
    options = {"settings_path": _write_claude_settings(n, ctx, worktree)} if native_claude else {}
    log = ctx.store.log_path(n)
    failure = ""
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
    size = issue_size(ctx.tracker.get_issue(n).labels)

    def implement(
        failure: str, *, merge_conflicts: list[str] | None = None, reviewer_text: str | None = None,
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
        prompt = build_prompt(
            n, parsed, ctx.config.acceptance_dir, failure, ctx.config.language,
            native_goal=native_claude or ctx.config.codex_goal, acceptance_locked=native_claude,
            merge_conflicts=merge_conflicts,
            reviewer_text=reviewer_text,
        )
        result = _available_executor(
            n, ctx, executor, worktree=worktree,
            prompt=prompt if native_claude else notes + prompt,
            model=model, timeout_s=remaining, log_path=log, **options,
        )
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
                checks.append(_check(command, worktree, log, 900))
                _progress(n, ctx)
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
            return ctx.tracker.create_pr(head=branch, base=target_branch, title=title, body=body)
        ctx.tracker.update_pr(pr, base=target_branch, title=title, body=body)
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
            pending_merge = True
            check_failure = f"Merge conflict with {target_branch}: {', '.join(conflicts)}"
            while True:
                run = ctx.store.get_run(n)
                _append(log, f"Attempt {run.attempts} failed:\n{check_failure}")
                if run.attempts >= ctx.config.max_retries + 1:
                    flag(ctx, n, Reason.RETRIES_EXCEEDED, "Checks or review failed after all attempts.", check_failure)
                    raise ExecutorStopped("retries-exceeded")
                ctx.store.update_run(n, attempts=run.attempts + 1)
                implement(check_failure, merge_conflicts=conflicts)
                # Retain the merge's paths even if the executor stages them.
                markers = git.conflict_marker_paths(conflicts)
                if markers:
                    check_failure = f"Conflict markers remain in: {', '.join(markers)}"
                    continue
                git.stage_changes()
                if pending_merge or git.has_staged_changes():
                    git.commit(title)
                    pending_merge = False
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
        git.push(branch)
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
                    flag(ctx, n, Reason.RETRIES_EXCEEDED, "CI failed", link)
                    return "ci-failed"
                remaining = deadline - ctx.now().timestamp()
                if remaining <= 0 or poll == 40:
                    flag(ctx, n, Reason.TIMEOUT, "CI did not finish", link)
                    return "timeout"
                ctx.sleep(min(30, remaining))

            target_sha = refresh_base()
            target_ref = f"origin/{target_branch}"
            if target_sha is not None:
                git.push(branch)
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
                    ctx.tracker.comment(n, f"PR #{pr} is waiting for your review\n\n{link}")
                    ctx.tracker.remove_labels(n, [RUNNING])
                    return "pr-open"
                continue
            _phase(n, ctx, "merging")
            try:
                ctx.tracker.merge_pr(pr, method="squash", expected_head_sha=verified_head_sha)
            except TrackerError:
                if ctx.tracker.pr_head_sha(pr) != verified_head_sha:
                    return _hold_changed_head(n, ctx, link)
                raise
            _progress(n, ctx)
            try:
                if target_branch == batch_branch:
                    ctx.tracker.close_issue(n, f"Merged into {batch_branch} (PR #{pr})")
                else:
                    ctx.tracker.close_issue(n)
            except TrackerError:
                if ctx.tracker.get_issue(n).state != "closed":
                    raise
            ctx.tracker.remove_labels(n, [READY, RUNNING])
            if target_branch == batch_branch and parent is not None:
                try:
                    _batch_pr(parent, ctx)
                except TrackerError as exc:
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
                git.push(branch)
                pr = open_or_update_pr(title, render_pr_body())
                _progress(n, ctx)
            review = _review(n, ctx, git, parsed, base_ref, round_number=len(reviews) + 1, pr=pr)
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
        if not failure or review_hold:
            try:
                return publish()
            except (AttemptFailure, PatchReplayError) as exc:
                failure = recover_replay(exc) if isinstance(exc, PatchReplayError) else str(exc)
                run = ctx.store.get_run(n)
        _append(log, f"Attempt {run.attempts} failed:\n{failure}")
        if run.attempts < ctx.config.max_retries + 1:
            ctx.store.update_run(n, attempts=run.attempts + 1)
            continue
        flag(ctx, n, Reason.RETRIES_EXCEEDED, "Checks or review failed after all attempts.", failure)
        # Preserve the failed implementation, rather than pushing an unchanged base.
        if paths and git.has_staged_changes():
            title = ctx.tracker.get_issue(n).title
            git.commit(f"{title} (#{n})")
        git.push(branch)
        return "retries-exceeded"


def run_issue(n: int, ctx: RunContext) -> str:
    outcome = "error"
    worktree = ctx.store.worktree_path(n)
    log = ctx.store.log_path(n)
    try:
        run = ctx.store.update_run(n, pid=os.getpid())
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
    except Exception as exc:
        _append(log, traceback.format_exc())
        flag(ctx, n, Reason.RETRIES_EXCEEDED, f"Runner failed: {' '.join(str(exc).split())}")
        outcome = "error"
    except SystemExit:
        outcome = "killed"
        raise
    finally:
        # A tick may already have finished the record while terminating this runner.
        try:
            run = ctx.store.get_run(n)
            if run is not None and run.status != "finished":
                ctx.store.finish_run(n, outcome=run.outcome or outcome)
        finally:
            kick_tick(ctx.repo_root)
    return outcome
