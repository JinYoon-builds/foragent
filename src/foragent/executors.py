"""CLI executor adapters; command construction is independent of execution."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile
from typing import Protocol

from foragent.processes import run_logged


@dataclass
class ExecResult:
    exit_code: int | None
    timed_out: bool
    last_message: str
    log_path: Path
    full_message: str | None = None
    log_tail: str = ""
    error_kind: str = "normal"
    session_id: str | None = None
    num_turns: int | None = None


def classify_result(result: ExecResult) -> str:
    """Only failed executions with structured availability signals may wait."""
    return result.error_kind if result.exit_code not in {None, 0} else "normal"


def _error_kind(message: str) -> str:
    """Interpret an executor error field, never assistant or tool output."""
    text = message.casefold()
    if re.search(r"rate[ _-]?limit|usage limit|\b429\b|quota.*(?:exceeded|exhausted)|insufficient_quota|limit will reset", text):
        return "limit"
    if re.search(
        r"at capacity|overloaded|\b(?:529|503)\b|service unavailable|econnreset|connection reset|stream disconnected", text,
    ):
        return "transient"
    return "normal"


def _events(path: Path, *, offset: int = 0):
    with path.open(encoding="utf-8", errors="replace") as stream:
        stream.seek(offset)
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def stream_session_id(path: Path, executor: str, *, offset: int = 0) -> str | None:
    """Read an initialized session's identity, never an error's echoed ID."""
    fallback = None
    started = False
    for event in _events(path, offset=offset):
        if executor == "codex":
            value = event.get("thread_id") if event.get("type") == "thread.started" else None
        else:
            if event.get("type") == "system" and event.get("subtype") == "init":
                started = True
                value = event.get("session_id")
            else:
                value = None
            if event.get("type") == "result":
                turns = event.get("num_turns")
                initialized = started or not event.get("is_error") or (isinstance(turns, int) and turns > 0)
                candidate = event.get("session_id")
                if initialized and isinstance(candidate, str) and candidate.strip():
                    fallback = candidate
        if isinstance(value, str) and value.strip():
            return value
    return fallback


def _event_error_kind(event: dict, executor: str) -> str:
    kind = event.get("type")
    if executor == "codex":
        if kind not in ("error", "turn.failed"):
            return "normal"
    else:
        if kind == "error" or (kind in (None, "result") and event.get("is_error") is True):
            status = str(event.get("api_error_status", "")).casefold()
            if status == "429":
                return "limit"
            if status in {"529", "503", "overloaded"}:
                return "transient"
        if kind != "error":
            return "normal"
    error = event.get("error")
    fields = [event.get("message")]
    if isinstance(error, dict):
        fields.append(error.get("message"))
        if executor == "claude":
            fields.append(error.get("type"))
    elif executor == "claude" and isinstance(error, str):
        fields.append(error)
    return _error_kind("\n".join(value for value in fields if isinstance(value, str)))


def _structured_error_kind(path: Path, executor: str, *, document: bool = False) -> str:
    if document:
        try:
            event = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            return "normal"
        events = [event] if isinstance(event, dict) else []
    else:
        events = _events(path)
    result = "normal"
    for event in events:
        kind = _event_error_kind(event, executor)
        if kind == "limit":
            return kind
        if kind == "transient":
            result = kind
    return result


class Executor(Protocol):
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path, resume_session_id: str | None = None,
    ) -> ExecResult: ...


def codex_command(worktree: Path, output_path: Path, model: str | None = None,
                  effort: str | None = None, *, resume_session_id: str | None = None) -> list[str]:
    # Resume has its own CLI options; cwd is supplied by run_logged.
    args = [
        "codex", "exec", "resume", resume_session_id, "-", "--skip-git-repo-check",
        "--json", "-o", str(output_path), "-c", 'sandbox_mode="workspace-write"',
    ] if resume_session_id is not None else [
        "codex", "exec", "-", "-C", str(worktree), "-s", "workspace-write",
        "--skip-git-repo-check", "--json", "-o", str(output_path),
    ]
    return args + (["-m", model] if model is not None else []) + (
        ["-c", f"model_reasoning_effort={effort}"] if effort is not None else []
    )


def claude_command(
    model: str | None = None, settings_path: Path | None = None, agent: str | None = None,
    *, resume_session_id: str | None = None,
) -> list[str]:
    args = ["claude", "-p", "--permission-mode", "auto", "--output-format", "stream-json", "--verbose"]
    if settings_path is not None:
        args += ["--settings", str(settings_path)]
    if agent is not None:
        args += ["--agent", agent]
    if resume_session_id is not None:
        args += ["--resume", resume_session_id]
    return args + (["--model", model] if model is not None else [])


def claude_review_command(level: str, model: str | None = None, *, pr: int | None = None) -> list[str]:
    target = f"{pr} " if pr is not None else ""
    args = ["claude", "-p", f"/code-review {target}{level}", "--permission-mode", "auto", "--output-format", "json"]
    return args + (["--model", model] if model is not None else [])


def claude_stream_result(path: Path) -> dict:
    """Read the last result event, without loading the session transcript."""
    result = {}
    for event in _events(path):
        if event.get("type") == "result":
            result = event
    return result


class CodexExecutor:
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path, effort: str | None = None,
        resume_session_id: str | None = None,
    ) -> ExecResult:
        with tempfile.TemporaryDirectory(prefix="foragent-codex-") as temporary:
            output_path = Path(temporary) / "last-message.txt"
            events_path = Path(temporary) / "events.jsonl"
            options = {"resume_session_id": resume_session_id} if resume_session_id is not None else {}
            code, timed_out, _ = run_logged(
                codex_command(worktree, output_path, model, effort, **options), cwd=worktree, prompt=prompt,
                timeout_s=timeout_s, log_path=log_path,
                capture_stdout=True, stdout_path=events_path,
            )
            last_message = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
            error_kind = _structured_error_kind(events_path, "codex")
            session_id = stream_session_id(events_path, "codex")
        return ExecResult(code, timed_out, last_message, log_path, last_message,
                          error_kind=error_kind, session_id=session_id)


class ClaudeExecutor:
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path,
        settings_path: Path | None = None, review_level: str | None = None,
        agent: str | None = None, review_pr: int | None = None,
        resume_session_id: str | None = None,
        goal: str | None = None,
    ) -> ExecResult:
        output_path = log_path.with_suffix(".stdout")
        options = {}
        if agent is not None:
            options["agent"] = agent
        if resume_session_id is not None:
            options["resume_session_id"] = resume_session_id
        command = (claude_review_command(review_level, model, pr=review_pr) if review_level is not None
                   else claude_command(model, settings_path, **options))
        if review_level is None and resume_session_id is None and goal is not None:
            # Text stdin is one message: blank lines do not end /goal's argument.
            # Stream input gives the command and task their own user turns.
            command += ["--input-format", "stream-json"]
            prompt = "".join(json.dumps({
                "type": "user", "message": {"role": "user", "content": content},
            }, ensure_ascii=False) + "\n" for content in (f"/goal {goal}", prompt))
        code, timed_out, _ = run_logged(
            command, cwd=worktree, prompt=None if review_level is not None else prompt, timeout_s=timeout_s,
            log_path=log_path, capture_stdout=True, stdout_path=output_path,
        )
        result = claude_stream_result(output_path) if review_level is None else {}
        value = result.get("result")
        message = (output_path.read_text(encoding="utf-8", errors="replace") if review_level is not None
                   else value if isinstance(value, str) else "")
        turns = result.get("num_turns")
        error_kind = _structured_error_kind(output_path, "claude", document=review_level is not None)
        session_id = stream_session_id(output_path, "claude") if review_level is None else None
        return ExecResult(code, timed_out, message, log_path, message,
                          error_kind=error_kind, session_id=session_id,
                          num_turns=turns if type(turns) is int else None)


def executor_for(name: str) -> Executor:
    if name == "codex":
        return CodexExecutor()
    if name == "claude":
        return ClaudeExecutor()
    raise ValueError(f"unknown executor: {name}")
