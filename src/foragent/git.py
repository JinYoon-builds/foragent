"""Small, injectable Git adapter for approval integrity checks."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


REVIEW_SCOPE_FILE = "CLAUDE.local.md"


class GitError(RuntimeError):
    """A Git command or remote URL could not be used."""


@dataclass(frozen=True)
class ReviewState:
    head_sha: str
    staged_tree: str | None
    files: dict[str, tuple[int, bytes | str] | None]


class GitRepo:
    def __init__(self, root: Path, run: Callable[..., str] | None = None):
        self.root = Path(root)
        self._run = run or self._run_git
        self.local_skill_paths: set[str] = set()

    def _run_git(self, args: list[str]) -> str:
        command = args
        while command[:1] == ["-C"]:
            command = command[2:]
        timeout = 300 if command and command[0] in {"fetch", "push"} else 120
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        ).stdout

    def _call(self, args: list[str]) -> str:
        try:
            return self._run(args)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            message = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise GitError(message or str(exc)) from exc

    def fetch(self, remote: str = "origin", ref: str | None = None) -> None:
        refspec = f"+refs/heads/{ref}:refs/remotes/{remote}/{ref}" if ref is not None else None
        self._call(["fetch", remote, *([refspec] if refspec is not None else [])])

    def at(self, root: Path) -> "GitRepo":
        return GitRepo(root, lambda args: self._call(["-C", str(root), *args]))

    def remove_worktree(self, path: Path) -> None:
        if path.exists():
            self._call(["worktree", "remove", "--force", str(path)])
        self._call(["worktree", "prune"])

    def add_worktree(self, path: Path, branch: str, base_ref: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call(["worktree", "add", "-B", branch, str(path), base_ref])

    def ref_sha(self, ref: str) -> str:
        return self._call(["rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()

    def add_detached_worktree(self, path: Path, ref: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call(["worktree", "add", "--detach", str(path), ref])

    def attach_branch(self, branch: str) -> None:
        """Continue work on a detached PR head without replacing its worktree."""
        self._call(["switch", "-C", branch, "HEAD"])

    def stage_changes(self) -> None:
        output = self._call([
            "ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z",
            "--", ".", ":!.foragent", f":!{REVIEW_SCOPE_FILE}",
            *(f":(exclude,literal){path}" for path in sorted(self.local_skill_paths)),
        ])
        paths = sorted(set(filter(None, output.split("\0"))))
        if paths:
            self._call(["--literal-pathspecs", "add", "-A", "--", *paths])
        self._exclude_artifacts()

    def _exclude_artifacts(self) -> None:
        # Also remove entries staged by an executor, retaining their local files.
        self._call(["rm", "--cached", "-r", "-f", "--ignore-unmatch", ".foragent"])
        if self.local_skill_paths:
            self._call(["--literal-pathspecs", "rm", "--cached", "-r", "-f", "--ignore-unmatch",
                        "--", *sorted(self.local_skill_paths)])
        if self._call(["ls-files", "--cached", "-z", "--", REVIEW_SCOPE_FILE]):
            self._call(["restore", "--staged", "--", REVIEW_SCOPE_FILE])

    def ignore_review_scope(self) -> None:
        # Linked worktrees use the common repository's info/exclude.
        path = Path(self._call(["rev-parse", "--git-path", "info/exclude"]).strip())
        if not path.is_absolute():
            path = self.root / path
        original = path.read_bytes() if path.exists() else b""
        rule = f"/{REVIEW_SCOPE_FILE}".encode()
        if rule not in original.splitlines():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original + (b"\n" if original and not original.endswith(b"\n") else b"") + rule + b"\n")

    def changed_paths(self, base_ref: str) -> list[str]:
        # NUL delimiters retain filenames containing tabs, quotes, or newlines.
        output = self._call(["diff", "--cached", "--name-only", "--no-renames", "-z", base_ref])
        return [path for path in output.split("\0") if path]

    def staged_tree_sha(self) -> str:
        return self._call(["write-tree"]).strip()

    def staged_diff(self, base_ref: str, *, binary: bool = False) -> str:
        return self._call(["diff", "--cached", "--no-ext-diff", "--no-textconv", *(["--binary"] if binary else []), base_ref])

    def write_staged_patch(self, base_ref: str, patch: Path) -> None:
        # Keep patch bytes intact even when source files are not UTF-8.
        self._call(["diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", f"--output={patch}", base_ref])

    def apply_staged_patch(self, patch: Path) -> None:
        self._call(["apply", "--3way", str(patch)])

    def _symlink_parent(self, name: str) -> Path | None:
        parent = self.root
        for part in Path(name).parts[:-1]:
            parent /= part
            if parent.is_symlink():
                return parent
        return None

    def review_state(self) -> ReviewState:
        paths = self._call(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        files = {}
        for name in filter(None, paths.split("\0")):
            path = self.root / name
            if parent := self._symlink_parent(name):
                files[parent.relative_to(self.root).as_posix()] = (parent.lstat().st_mode, os.readlink(parent))
                files[name] = None
                continue
            try:
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    files[name] = (mode, os.readlink(path))
                elif stat.S_ISREG(mode):
                    files[name] = (mode, path.read_bytes())
            except (FileNotFoundError, NotADirectoryError):
                files[name] = None
        try:
            tree = self.staged_tree_sha()
        except GitError as exc:
            if "unmerged" not in str(exc):
                raise
            tree = None
        return ReviewState(self.head_sha(), tree, files)

    def restore_review_state(self, before: ReviewState) -> None:
        """Restore HEAD and index, then undo the reviewer's working tree edits."""
        after = self.review_state()
        if before.staged_tree is None:
            raise GitError("Cannot restore an unmerged baseline index")
        if after.head_sha != before.head_sha:
            self._call(["update-ref", "HEAD", before.head_sha, after.head_sha])
        self._call(["read-tree", before.staged_tree])
        changed = {name for name in before.files.keys() | after.files.keys()
                   if before.files.get(name) != after.files.get(name)}
        for name in sorted(changed, key=lambda name: name.count("/"), reverse=True):
            path = self.root / name
            if self._symlink_parent(name) is not None:
                continue  # Remove the replacement link itself, never its target.
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                try:
                    path.unlink(missing_ok=True)
                except NotADirectoryError:
                    pass  # A parent directory was replaced by a file.
        for name in sorted(changed):
            saved = before.files.get(name)
            if saved is None:
                continue
            mode, content = saved
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISLNK(mode):
                path.symlink_to(content)
            else:
                path.write_bytes(content)
                path.chmod(stat.S_IMODE(mode))

    def ensure_batch_branch(self, branch: str, base_ref: str) -> None:
        """Create a remote batch once; never force-push an existing batch."""
        remote_ref = f"refs/heads/{branch}"
        if not self._call(["ls-remote", "--heads", "origin", remote_ref]).strip():
            self._call(["push", "origin", f"{base_ref}:{remote_ref}"])
        self.fetch("origin", branch)

    def head_sha(self) -> str:
        return self._call(["rev-parse", "HEAD"]).strip()

    def is_ancestor(self, ancestor: str, head: str) -> bool:
        try:
            self._call(["merge-base", "--is-ancestor", ancestor, head])
        except GitError as exc:
            if isinstance(exc.__cause__, subprocess.CalledProcessError) and exc.__cause__.returncode == 1:
                return False
            raise
        return True

    def merge_base(self, target: str) -> None:
        self._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                    "merge", "--no-edit", target])

    def conflict_paths(self) -> list[str]:
        return list(filter(None, self._call(["diff", "--name-only", "--diff-filter=U", "-z"]).split("\0")))

    def conflict_marker_paths(self, paths: Iterable[str]) -> list[str]:
        remaining = []
        for name in sorted(set(paths)):
            path = self.root / name
            if path.is_symlink() or not path.is_file():
                continue
            value = self._call(["check-attr", "-z", "conflict-marker-size", "--", name]).split("\0")[2]
            try:
                width = int(value)
            except ValueError:
                width = 7
            if width <= 0:
                width = 7
            marker = re.compile(
                rf"^(?:<{{{width}}}|\|{{{width}}}|={{{width}}}|>{{{width}}})(?:[ \t].*)?\r?$".encode(),
                re.MULTILINE,
            )
            if marker.search(path.read_bytes()):
                remaining.append(name)
        return remaining

    def has_staged_changes(self) -> bool:
        return self.staged_tree_sha() != self._call(["rev-parse", "HEAD^{tree}"]).strip()

    def commit(self, title: str) -> None:
        self._exclude_artifacts()
        self._call([
            "-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
            "commit", "-m", title,
        ])

    def continue_merge(self) -> None:
        """Conclude a resolved merge, retaining its parents and clearing merge state."""
        self._exclude_artifacts()
        self._call([
            "-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
            "-c", "core.editor=true", "merge", "--continue",
        ])

    def remote_branch_sha(self, branch: str) -> str | None:
        output = self._call(["ls-remote", "--heads", "origin", f"refs/heads/{branch}"])
        return output.split()[0] if output.strip() else None

    def push(self, branch: str, *, lease: str | None = None) -> None:
        force = "-f" if lease is None else f"--force-with-lease=refs/heads/{branch}:{lease}"
        self._call(["push", force, "origin", branch])

    def check_worktree(self) -> None:
        try:
            inside = self._call(["rev-parse", "--is-inside-work-tree"]).strip()
        except GitError as exc:
            raise GitError(f"repo-root must be a git work tree: {exc}") from exc
        if inside != "true":
            raise GitError("repo-root must be a git work tree")

    def current_branch(self) -> str:
        try:
            return self._call(["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
        except GitError as exc:
            raise GitError("init requires the base branch; HEAD is detached") from exc

    def worktree_root(self) -> Path:
        return Path(self._call(["rev-parse", "--show-toplevel"]).strip()).resolve()

    def uncommitted_paths(self, paths: list[str]) -> list[str]:
        output = self._call([
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", *paths,
        ])
        return [entry[3:] for entry in output.split("\0") if entry]

    def ensure_tracked(self, paths: list[str]) -> None:
        """Include explicitly published files even when ignore rules hide them."""
        paths = [path for path in paths if Path(path).as_posix() != REVIEW_SCOPE_FILE]
        if not paths:
            return

        def missing() -> list[str]:
            tracked = set(self._call(["ls-files", "--cached", "-z", "--", *paths]).split("\0"))
            return sorted(set(paths) - tracked)

        if untracked := missing():
            self._call(["add", "-f", "--", *untracked])
            if untracked := missing():
                raise GitError(f"published files are not tracked: {', '.join(untracked)}")

    def has_unpushed_setup(self, branch: str) -> bool:
        local = f"refs/heads/{branch}"
        remote = f"refs/remotes/origin/{branch}"
        try:
            self._call(["rev-parse", "--verify", "--quiet", remote])
        except GitError as exc:
            cause = exc.__cause__
            if not isinstance(cause, subprocess.CalledProcessError) or cause.returncode != 1:
                raise
            revision_range = local
        else:
            counts = self._call(["rev-list", "--left-right", "--count", f"{remote}...{local}"])
            behind, ahead = map(int, counts.split())
            if behind or not ahead:
                return False
            revision_range = f"{remote}..{local}"
        subjects = self._call(["log", "--format=%s", revision_range]).splitlines()
        return "Set up Foragent" in subjects

    def commit_paths(self, paths: list[str], title: str) -> None:
        """Commit only setup paths, preserving unrelated staged changes."""
        paths = [path for path in paths if Path(path).as_posix() != REVIEW_SCOPE_FILE]
        if not paths:
            self._exclude_artifacts()
            return
        identity = []
        for key, fallback in (("user.name", "foragent"), ("user.email", "foragent@localhost")):
            try:
                value = self._call(["config", "--get", key]).strip()
            except GitError as exc:
                cause = exc.__cause__
                if not isinstance(cause, subprocess.CalledProcessError) or cause.returncode != 1:
                    raise
                value = ""
            identity.extend(["-c", f"{key}={value or fallback}"])
        self._call(["add", "-f", "--", *paths])
        self._exclude_artifacts()
        self._call([*identity, "commit", "--only", "-m", title, "--", *paths, f":!{REVIEW_SCOPE_FILE}"])

    def push_base(self, branch: str) -> None:
        self._call(["push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"])

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
