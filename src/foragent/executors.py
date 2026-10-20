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


def _events(path: Path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


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
        timeout_s: int, log_path: Path,
    ) -> ExecResult: ...


def codex_command(worktree: Path, output_path: Path, model: str | None = None,
                  effort: str | None = None) -> list[str]:
    args = [
        "codex", "exec", "-", "-C", str(worktree), "-s", "workspace-write",
        "--skip-git-repo-check", "--json", "-o", str(output_path),
    ]
    return args + (["-m", model] if model is not None else []) + (
        ["-c", f"model_reasoning_effort={effort}"] if effort is not None else []
    )


def claude_command(
    model: str | None = None, settings_path: Path | None = None, agent: str | None = None,
) -> list[str]:
    args = ["claude", "-p", "--permission-mode", "auto", "--output-format", "stream-json", "--verbose"]
    if settings_path is not None:
        args += ["--settings", str(settings_path)]
    if agent is not None:
        args += ["--agent", agent]
    return args + (["--model", model] if model is not None else [])


def claude_review_command(level: str, model: str | None = None) -> list[str]:
    args = ["claude", "-p", f"/code-review {level}", "--permission-mode", "auto", "--output-format", "json"]
    return args + (["--model", model] if model is not None else [])


def claude_stream_result(path: Path) -> str:
    """Read the last result event, without loading the session transcript."""
    result = ""
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                value = event.get("result")
                result = value if isinstance(value, str) else ""
    return result


class CodexExecutor:
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path, effort: str | None = None,
    ) -> ExecResult:
        with tempfile.TemporaryDirectory(prefix="foragent-codex-") as temporary:
            output_path = Path(temporary) / "last-message.txt"
            events_path = Path(temporary) / "events.jsonl"
            code, timed_out, _ = run_logged(
                codex_command(worktree, output_path, model, effort), cwd=worktree, prompt=prompt,
                timeout_s=timeout_s, log_path=log_path,
                capture_stdout=True, stdout_path=events_path,
            )
            last_message = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
            error_kind = _structured_error_kind(events_path, "codex")
        return ExecResult(code, timed_out, last_message, log_path, last_message, error_kind=error_kind)


class ClaudeExecutor:
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path,
        settings_path: Path | None = None, review_level: str | None = None,
        agent: str | None = None,
    ) -> ExecResult:
        output_path = log_path.with_suffix(".stdout")
        command = (claude_review_command(review_level, model) if review_level is not None
                   else claude_command(model, settings_path, agent) if agent is not None
                   else claude_command(model, settings_path))
        code, timed_out, _ = run_logged(
            command, cwd=worktree, prompt=None if review_level is not None else prompt, timeout_s=timeout_s,
            log_path=log_path, capture_stdout=True, stdout_path=output_path,
        )
        message = (output_path.read_text(encoding="utf-8", errors="replace") if review_level is not None
                   else claude_stream_result(output_path))
        error_kind = _structured_error_kind(output_path, "claude", document=review_level is not None)
        return ExecResult(code, timed_out, message, log_path, message, error_kind=error_kind)


def executor_for(name: str) -> Executor:
    if name == "codex":
        return CodexExecutor()
    if name == "claude":
        return ClaudeExecutor()
    raise ValueError(f"unknown executor: {name}")
