"""CLI executor adapters; command construction is independent of execution."""

from dataclasses import dataclass
from pathlib import Path
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


class Executor(Protocol):
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path,
    ) -> ExecResult: ...


def codex_command(worktree: Path, output_path: Path, model: str | None = None) -> list[str]:
    args = [
        "codex", "exec", "-", "-C", str(worktree), "-s", "workspace-write",
        "--skip-git-repo-check", "--json", "-o", str(output_path),
    ]
    return args + (["-m", model] if model is not None else [])


def claude_command(model: str | None = None) -> list[str]:
    args = ["claude", "-p", "--permission-mode", "auto", "--output-format", "text"]
    return args + (["--model", model] if model is not None else [])


class CodexExecutor:
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path,
    ) -> ExecResult:
        with tempfile.TemporaryDirectory(prefix="foragent-codex-") as temporary:
            output_path = Path(temporary) / "last-message.txt"
            code, timed_out, _ = run_logged(
                codex_command(worktree, output_path, model), cwd=worktree, prompt=prompt,
                timeout_s=timeout_s, log_path=log_path,
            )
            last_message = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
        return ExecResult(code, timed_out, last_message, log_path, last_message)


class ClaudeExecutor:
    def run(
        self, *, worktree: Path, prompt: str, model: str | None,
        timeout_s: int, log_path: Path,
    ) -> ExecResult:
        output_path = log_path.with_suffix(".stdout")
        code, timed_out, tail = run_logged(
            claude_command(model), cwd=worktree, prompt=prompt, timeout_s=timeout_s,
            log_path=log_path, capture_stdout=True, stdout_path=output_path,
        )
        return ExecResult(code, timed_out, tail, log_path, output_path.read_text(encoding="utf-8", errors="replace"))


def executor_for(name: str) -> Executor:
    if name == "codex":
        return CodexExecutor()
    if name == "claude":
        return ClaudeExecutor()
    raise ValueError(f"unknown executor: {name}")
