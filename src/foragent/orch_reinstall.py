"""Refresh the installed Foragent tool after a successful self-merge."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from time import sleep
import tomllib
from typing import TYPE_CHECKING

from foragent import tool_install
from foragent.orch_events import append_event

if TYPE_CHECKING:
    from foragent.runner import RunContext


def _uv(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    """Shared uv boundary, including retries for uv's directory-removal race."""
    for attempt in range(1, 4):
        result = subprocess.run(
            ["uv", *args], cwd=cwd, env=env, capture_output=True, text=True,
            errors="replace", timeout=300,
        )
        if not result.returncode:
            return result.stdout.strip()
        output = f"{result.stderr}\n{result.stdout}"
        if attempt == 3 or not any(error in output for error in (
            "Directory not empty", "failed to remove directory",
        )):
            raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                               or f"uv {' '.join(args)} exited {result.returncode}")
        sleep(2.0 * attempt)
    raise AssertionError("unreachable")


def _reinstall(ctx: "RunContext", base: str) -> str | None:
    ctx.git.fetch("origin", base)
    sha = ctx.git.ref_sha(f"refs/remotes/origin/{base}")
    with TemporaryDirectory(prefix="foragent-reinstall-") as directory:
        worktree = Path(directory) / "worktree"
        ctx.git.add_detached_worktree(worktree, sha)
        try:
            project_file = worktree / "pyproject.toml"
            if not project_file.is_file():
                return None
            with project_file.open("rb") as stream:
                project = tomllib.load(stream).get("project", {})
            if project.get("name") != "foragent":
                return None
            source = worktree / "src" / "foragent"
            if not source.is_dir():
                raise RuntimeError(f"package directory missing: {source}")
            tool_install.install(worktree, cwd=worktree, revision=sha,
                                 verify=lambda directory: tool_install.verify_package(source, directory))
        finally:
            ctx.git.remove_worktree(worktree)
    return f"reinstalled Foragent {sha}; installed package matches merged source"


def after_merge(ctx: "RunContext", pr: int, base: str) -> None:
    """Keep the successful merge even if installing or verifying fails."""
    try:
        message = _reinstall(ctx, base)
    except Exception as exc:
        reason = " ".join(str(exc).split()) or type(exc).__name__
        print(f"reinstall failed: {reason}")
        append_event(ctx.store, ctx.now(), None, "reinstall-failed", f"PR #{pr}: {reason}")
    else:
        if message is not None:
            print(message)
