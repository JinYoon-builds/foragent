"""Small, injectable Git adapter for approval integrity checks."""

from collections.abc import Callable, Iterable
from pathlib import Path
import re
import subprocess


class GitError(RuntimeError):
    """A Git command or remote URL could not be used."""


class GitRepo:
    def __init__(self, root: Path, run: Callable[..., str] | None = None):
        self.root = Path(root)
        self._run = run or self._run_git

    def _run_git(self, args: list[str]) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout

    def _call(self, args: list[str]) -> str:
        try:
            return self._run(args)
        except (OSError, subprocess.CalledProcessError) as exc:
            message = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise GitError(message or str(exc)) from exc

    def fetch(self, remote: str = "origin", ref: str | None = None) -> None:
        refspec = f"+refs/heads/{ref}:refs/remotes/{remote}/{ref}" if ref is not None else None
        self._call(["fetch", remote, *([refspec] if refspec is not None else [])])

    def commit_before(self, ref: str, iso_time: str) -> str | None:
        return self._call(["rev-list", "-1", f"--before={iso_time}", ref]).strip() or None

    def blob_sha(self, ref: str, path: str) -> str | None:
        try:
            return self._call(["rev-parse", f"{ref}:{path}"]).strip()
        except GitError as exc:
            cause = exc.__cause__
            if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 128:
                message = cause.stderr or ""
                if "does not exist in" in message or "exists on disk, but not in" in message:
                    return None
            raise

    def blob_shas(self, ref: str, paths: Iterable[str]) -> dict[str, str]:
        return {path: self.blob_sha(ref, path) or "missing" for path in paths}

    def remote_slug(self, remote: str = "origin") -> str:
        url = self._call(["remote", "get-url", remote]).strip()
        match = re.fullmatch(
            r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)"
            r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", url,
        )
        if match is None:
            raise GitError(f"cannot determine owner/name from remote {remote}; set repo in .foragent.yml")
        return f"{match[1]}/{match[2]}"
