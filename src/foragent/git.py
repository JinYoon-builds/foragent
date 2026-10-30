"""Small, injectable Git adapter for approval integrity checks."""

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from foragent.state import StateStore


REVIEW_SCOPE_FILE = "CLAUDE.local.md"
# A split part names its source when at least half of its added lines (of at
# least this many, each this long once stripped) come from that source.
SPLIT_MIN_LINES = 5
SPLIT_MIN_LINE = 4
ARTIFACT_PATHS = (".foragent",)


def _conflict_marker_numbers(content: str, widths: set[int]) -> list[int]:
    """Report marker lines only when an opening is followed by a closing."""
    openings = tuple("<" * width for width in widths)
    closings = tuple(">" * width for width in widths)
    ancestors = tuple("|" * width for width in widths)
    separators = {"=" * width for width in widths}
    remaining, pending = [], []
    for number, line in enumerate(content.splitlines(), 1):
        if line.startswith(openings):
            pending.append(number)
        elif line.startswith(closings):
            if pending:
                remaining.extend([*pending, number])
                pending = []
        elif pending and (line.startswith(ancestors) or line in separators):
            pending.append(number)
    return remaining


class GitError(RuntimeError):
    """A Git command or remote URL could not be used."""


class PublicationError(GitError):
    """The proposed Git contents contain unresolved conflict markers."""

    def __init__(self, markers: list[tuple[str, int]], language: str = "en"):
        from foragent.i18n import text

        self.markers = markers
        super().__init__("\n".join(text("conflict_marker_left", language, location=f"{path}:{line}")
                                   for path, line in markers))


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
        self.publication_paths: set[str] = set()

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
        for attempt in range(3):
            try:
                self._call(["fetch", remote, *([refspec] if refspec is not None else [])])
                return
            except GitError as exc:
                # Another fetch can update the shared remote ref after Git reads it.
                if "incorrect old value provided" not in str(exc) or attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def at(self, root: Path) -> "GitRepo":
        return GitRepo(root, lambda args: self._call(["-C", str(root), *args]))

    def remove_worktree(self, path: Path) -> None:
        with ExitStack() as stack:
            # Every issue-worktree cleanup path goes through this adapter,
            # including runner exit, rebuild, tick recovery and explicit kills.
            if path.parent.name == "worktrees" and path.name.startswith("issue-") and path.name[6:].isdigit():
                store = StateStore(path.parent.parent)
                stack.enter_context(store.worktree_lock(int(path.name[6:])))
            if path.exists():
                self._call(["worktree", "remove", "--force", str(path)])
            self._call(["worktree", "prune"])

    def add_worktree(self, path: Path, branch: str, base_ref: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call(["worktree", "add", "-B", branch, str(path), base_ref])

    def ref_sha(self, ref: str) -> str:
        return self._call(["rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()

    def union_merge_paths(self, base_ref: str, paths: Iterable[str]) -> set[str]:
        """Read merge attributes in one batch; unreadable attributes retain overlap limits."""
        paths = sorted(set(paths))
        if not paths:
            return set()
        try:
            fields = self._call(["check-attr", f"--source={base_ref}", "-z", "merge", "--", *paths]).split("\0")
        except GitError:
            return set()
        return {path for path, attribute, value in zip(fields[::3], fields[1::3], fields[2::3])
                if attribute == "merge" and value == "union" and path in paths}

    def latest_trailer(self, key: str, base_ref: str) -> str | None:
        """Read the newest carrying commit on this branch's first-parent chain."""
        fields = self._call([
            "log", "--first-parent",
            f"--format=%(trailers:key={key},keyonly)%x00%(trailers:key={key},valueonly)%x00",
            f"{base_ref}..HEAD",
        ]).split("\0")
        for index in range(0, len(fields) - 1, 2):
            if fields[index].strip():
                return fields[index + 1].strip()
        return None

    def local_branch_sha(self, branch: str) -> str | None:
        try:
            return self._call(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).strip()
        except GitError as exc:
            cause = exc.__cause__
            if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1:
                return None
            raise

    def add_detached_worktree(self, path: Path, ref: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call(["worktree", "add", "--detach", str(path), ref])

    def attach_branch(self, branch: str) -> None:
        """Continue work on a detached PR head without replacing its worktree."""
        self._call(["switch", "-C", branch, "HEAD"])

    def stage_changes(self) -> None:
        output = self._call([
            "ls-files", "--modified", "--deleted", "--others", "--exclude-standard", "-z",
            "--", ".", *(f":!{path}" for path in ARTIFACT_PATHS), f":!{REVIEW_SCOPE_FILE}",
            *(f":(exclude,literal){path}" for path in sorted(self.local_skill_paths)),
        ])
        paths = sorted(set(filter(None, output.split("\0"))))
        if paths:
            self._call(["--literal-pathspecs", "add", "-A", "--", *paths])
        self._exclude_artifacts()

    def reconcile_local_skills(self, target_ref: str) -> None:
        """Release injected dependencies only after clearing copies owned by target."""
        owned = {name for name in self.local_skill_paths if self.blob_sha(target_ref, name) is not None}
        for name in sorted(owned):
            # A tracked HEAD copy already belongs to the repository. Otherwise
            # remove the injected directory (including executor-staged files) so
            # the target merge can install its tree without an untracked collision.
            if self.blob_sha("HEAD", name) is None:
                path = self.root / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                self._call(["--literal-pathspecs", "rm", "--cached", "-r", "-f", "--ignore-unmatch", "--", name])
        self.local_skill_paths.difference_update(owned)

    def artifact_paths(self) -> set[str]:
        """Runner-owned roots intentionally removed from the index."""
        return {*ARTIFACT_PATHS, *self.local_skill_paths}

    def _exclude_artifacts(self) -> None:
        # Also remove entries staged by an executor, retaining their local files.
        self._call(["rm", "--cached", "-r", "-f", "--ignore-unmatch", *ARTIFACT_PATHS])
        # Retain runner-owned protocol inputs explicitly preserved by the caller.
        self.ensure_tracked(sorted(self.publication_paths))
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

    def _own_base(self, base_ref: str) -> str:
        # A fetched target can move independently of this branch. Compare the
        # index with their shared history so target-only changes are excluded.
        # Pending merge parents already contribute to the index before HEAD moves.
        parents = ["HEAD", "MERGE_HEAD"] if self.merge_in_progress() else ["HEAD"]
        try:
            return self._call(["merge-base", base_ref, *parents]).strip()
        except GitError as exc:
            if not isinstance(exc.__cause__, subprocess.CalledProcessError) or exc.__cause__.returncode != 1:
                raise
            return base_ref  # Unrelated histories have no merge base.

    def changed_paths(self, base_ref: str) -> list[str]:
        # NUL delimiters retain filenames containing tabs, quotes, or newlines.
        output = self._call(["diff", "--cached", "--name-only", "--no-renames", "-z", self._own_base(base_ref)])
        return [path for path in output.split("\0") if path]

    def committed_diff(self, base: str, head: str) -> list[tuple[bytes, bytes, bytes, bytes, bytes]]:
        """Raw `diff-tree` entries from base to head (commits or trees), never decoded.

        Each entry is (old mode, new mode, old sha, new sha, path bytes). No
        rename detection, so a rename is its deletion and addition; blobs are
        never read, so this lists exactly what Git records.
        """
        try:
            output = subprocess.run(
                ["git", "-c", "core.precomposeUnicode=false", "diff-tree", "-r", "-z", "--no-renames", "--raw",
                 base, head], cwd=self.root, check=True, capture_output=True, timeout=120).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitError(str(getattr(exc, "stderr", b"") or exc)) from exc
        tokens = output.split(b"\0")
        entries = []
        for i in range(0, len(tokens) - 1, 2):
            meta, path = tokens[i], tokens[i + 1]
            old_mode, new_mode, old_sha, new_sha, _status = meta.lstrip(b":").split(b" ")
            entries.append((old_mode, new_mode, old_sha, new_sha, path))
        return entries

    def blob_bytes(self, sha: bytes) -> bytes:
        """A blob's exact bytes (git cat-file applies no filter)."""
        try:
            return subprocess.run(["git", "cat-file", "blob", sha.decode()], cwd=self.root, check=True,
                                  capture_output=True, timeout=120).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitError(str(getattr(exc, "stderr", b"") or exc)) from exc

    def staged_tree_sha(self) -> str:
        return self._call(["write-tree"]).strip()

    def restore_staged_paths(self, ref: str, paths: Iterable[str]) -> None:
        paths = list(paths)
        if not paths:
            return
        # restore rejects paths absent from both trees, even when other paths
        # need restoring. Index-only entries must remain in the list to remove
        # additions whose approved state is absence.
        source = self._call(["--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z", ref, "--", *paths])
        indexed = self._call(["--literal-pathspecs", "ls-files", "--cached", "-z", "--", *paths])
        known = set(source.split("\0")) | set(indexed.split("\0"))
        paths = [path for path in paths if path in known]
        if paths:
            self._call(["--literal-pathspecs", "restore", "--source", ref, "--staged", "--", *paths])

    def staged_diff(self, base_ref: str, *, binary: bool = False) -> str:
        return self._call(["diff", "--cached", "--no-ext-diff", "--no-textconv", *(["--binary"] if binary else []), self._own_base(base_ref)])

    def write_staged_patch(self, base_ref: str, patch: Path) -> None:
        # Keep patch bytes intact even when source files are not UTF-8.
        self._call(["diff", "--cached", "--no-ext-diff", "--no-textconv", "--binary", f"--output={patch}", self._own_base(base_ref)])

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

    def commit_recorded(self, paths: list[str], title: str, record: Callable[[str], None]) -> str:
        """Commit only ``paths`` (like commit_paths), handing the new sha to ``record`` before HEAD moves.

        The commit is built from HEAD plus these paths in a private index, so other staged
        changes stay out. After ``record`` returns, HEAD moves to it only if HEAD has not
        moved meanwhile, and these paths' index entries are set to the new commit.
        """
        parent = self.head_sha()
        git_dir = Path(self._call(["rev-parse", "--git-dir"]).strip())
        index = (git_dir if git_dir.is_absolute() else self.root / git_dir) / "foragent-batch-index"
        environment = {**os.environ, "GIT_INDEX_FILE": str(index)}

        def git(*args: str) -> str:
            try:
                return subprocess.run(["git", *args], cwd=self.root, env=environment, check=True,
                                      capture_output=True, text=True, timeout=120).stdout
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise GitError(getattr(exc, "stderr", "") or str(exc)) from exc

        try:
            git("read-tree", parent)
            present = [path for path in paths if (self.root / path).exists()]
            if present:
                git("--literal-pathspecs", "add", "-f", "--", *present)
            if missing := [path for path in paths if path not in present]:
                git("--literal-pathspecs", "rm", "--cached", "-q", "--ignore-unmatch", "--", *missing)
            tree = git("write-tree").strip()
        finally:
            index.unlink(missing_ok=True)
        identity = []
        for key, fallback in (("user.name", "foragent"), ("user.email", "foragent@localhost")):
            try:
                value = self._call(["config", "--get", key]).strip()
            except GitError:
                value = ""
            identity.extend(["-c", f"{key}={value or fallback}"])
        sha = self._call([*identity, "commit-tree", tree, "-p", parent, "-m", title]).strip()
        record(sha)
        self._call(["update-ref", "-m", title.splitlines()[0], "HEAD", sha, parent])
        self.restore_staged_paths("HEAD", paths)
        return sha

    def is_ancestor(self, ancestor: str, head: str) -> bool:
        try:
            self._call(["merge-base", "--is-ancestor", ancestor, head])
        except GitError as exc:
            if isinstance(exc.__cause__, subprocess.CalledProcessError) and exc.__cause__.returncode == 1:
                return False
            raise
        return True

    def merge_base(self, target: str, *, commit: bool = True) -> None:
        self._call(["-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
                    "merge", "--no-edit", *([] if commit else ["--no-commit"]), target])

    def moved_by(self, target: str) -> list[tuple[str, str]]:
        """Files this branch changed that ``target`` renamed or copied (split) since the merge base.

        Git rename and copy detection pairs each such file with where its content went.
        """
        try:
            base = self._call(["merge-base", "HEAD", target]).strip()
        except GitError:
            return []
        mine = {path for path in self._call(["diff", "--name-only", "--no-renames", "-z", base, "HEAD"]).split("\0")
                if path}
        return self.moved_between(base, target, mine)

    def moved_between(self, base: str, target: str, paths: Iterable[str]) -> list[tuple[str, str]]:
        """Which of ``paths`` ``target`` renamed or copied since its merge base with ``base``: (old, new).

        Copies from unchanged files count too, so a module split out while the original stays is named.
        """
        mine = set(paths)
        if not mine:
            return []
        try:
            base = self._call(["merge-base", base, target]).strip()
        except GitError:
            return []
        fields = iter(self._call(["diff", "--name-status", "-M", "-C", "--find-copies-harder", "-z",
                                  base, target]).split("\0"))
        moved = []
        for status in fields:
            if status[:1] in ("R", "C"):
                old, new = next(fields, ""), next(fields, "")
                if old in mine:
                    moved.append((old, new))
            elif status:
                next(fields, None)
        found = set(moved)
        return moved + [pair for pair in self._split_into(base, target, mine) if pair not in found]

    def _split_into(self, base: str, target: str, mine: set[str]) -> list[tuple[str, str]]:
        """Files whose added lines mostly come from one of ``mine``: parts of a split file.

        Git's copy detection needs a whole-file similarity; a module split into
        three parts leaves each part near a third and is not reported.
        """
        def lines(ref: str, path: str) -> list[str]:
            try:
                text = self._call(["show", f"{ref}:{path}"])
            except (GitError, UnicodeDecodeError):
                return []
            return [line.strip() for line in text.splitlines() if len(line.strip()) >= SPLIT_MIN_LINE]

        fields = iter(self._call(["diff", "--name-status", "--no-renames", "-z", base, target]).split("\0"))
        changed = {}
        for status in fields:
            if status:
                changed[next(fields, "")] = status[:1]
        sources = {path: set(lines(base, path)) for path in sorted(mine) if changed.get(path) != "A"}
        sources = {path: content for path, content in sources.items() if content}
        pairs = []
        for dest, status in sorted(changed.items()):
            if status not in ("A", "M") or not sources:
                continue
            added = Counter(lines(target, dest))
            if status == "M":
                added -= Counter(lines(base, dest))
            total = sum(added.values())
            if total < SPLIT_MIN_LINES:
                continue
            for source, content in sources.items():
                if source != dest and sum(count for line, count in added.items() if line in content) * 2 >= total:
                    pairs.append((source, dest))
        return pairs

    def merge_in_progress(self) -> bool:
        path = Path(self._call(["rev-parse", "--git-path", "MERGE_HEAD"]).strip())
        return (path if path.is_absolute() else self.root / path).is_file()

    def quit_merge(self) -> None:
        """Forget merge parents while retaining the index and partial resolution."""
        self._call(["merge", "--quit"])

    def conflict_paths(self) -> list[str]:
        return list(filter(None, self._call(["diff", "--name-only", "--diff-filter=U", "-z"]).split("\0")))

    def _conflict_marker_width(self, name: str) -> int:
        value = self._call(["check-attr", "-z", "conflict-marker-size", "--", name]).split("\0")[2]
        try:
            width = int(value)
        except ValueError:
            width = 7
        return width if width > 0 else 7

    def conflict_marker_paths(self, paths: Iterable[str]) -> list[str]:
        remaining = []
        for name in sorted(set(paths)):
            path = self.root / name
            if path.is_symlink() or not path.is_file():
                continue
            widths = {7, self._conflict_marker_width(name)}
            if _conflict_marker_numbers(path.read_text(errors="replace"), widths):
                remaining.append(name)
        return remaining

    def conflict_marker_lines(self, paths: Iterable[str]) -> list[tuple[str, int]]:
        """Locate Git markers in the index contents that would be published."""
        paths = sorted(set(paths))
        if not paths:
            return []
        # Git does not generate conflict marker lines in binary blobs. Ask Git
        # which index entries are text before reading them through the text adapter.
        try:
            text_paths = self._call(["--literal-pathspecs", "grep", "--cached", "-I", "-l", "-z",
                                     "-e", "", "--", *paths])
        except GitError as exc:
            if isinstance(exc.__cause__, subprocess.CalledProcessError) and exc.__cause__.returncode == 1:
                return []
            raise
        paths = list(filter(None, text_paths.split("\0")))
        if not paths:
            return []
        entries = self._call(["--literal-pathspecs", "ls-files", "--stage", "-z", "--", *paths])
        remaining = []
        for entry in filter(None, entries.split("\0")):
            metadata, name = entry.split("\t", 1)
            mode, sha, stage = metadata.split()
            if mode not in {"100644", "100755"} or stage != "0":
                continue
            widths = {7, self._conflict_marker_width(name)}
            remaining.extend((name, number) for number in _conflict_marker_numbers(self._call(["cat-file", "blob", sha]), widths))
        return remaining

    def check_publication(self, base_ref: str) -> None:
        """Validate staged PR contents in this branch's own changed files."""
        markers = self.conflict_marker_lines(self.changed_paths(base_ref))
        if markers:
            raise PublicationError(markers, self._language())

    def _language(self) -> str:
        """Project language for diagnostics; English when the config cannot be read."""
        from foragent.config import load_config

        try:
            return load_config(self.root).language
        except Exception:
            return "en"

    def has_staged_changes(self) -> bool:
        return self.staged_tree_sha() != self._call(["rev-parse", "HEAD^{tree}"]).strip()

    def staged_deletions(self) -> list[str]:
        output = self._call(["diff", "--cached", "--name-only", "--no-renames", "--diff-filter=D", "-z"])
        return list(filter(None, output.split("\0")))

    def committed_paths(self, base_ref: str) -> set[str]:
        """Branch changes since the shared base, including earlier deletions."""
        output = self._call(["diff", "--name-only", "--no-renames", "-z", self._own_base(base_ref), "HEAD"])
        # Exclude the target's history, including commits already merged from it.
        output += self._call(["log", "--format=", "--name-only", "--no-renames", "--diff-filter=D",
                              "-z", f"{base_ref}..HEAD"])
        return set(filter(None, output.split("\0")))

    def commit(self, title: str, *, allow_empty: bool = False) -> None:
        self._exclude_artifacts()
        self._call([
            "-c", "user.name=foragent", "-c", "user.email=foragent@localhost",
            "commit", *(["--allow-empty"] if allow_empty else []), "-m", title,
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

    def push_head(self, branch: str, *, lease: str) -> None:
        """Push a detached HEAD to ``branch`` only while the remote still has ``lease``."""
        self._call(["push", f"--force-with-lease=refs/heads/{branch}:{lease}", "origin", f"HEAD:refs/heads/{branch}"])

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
        # Already-staged deletions belong in the commit, but git add cannot
        # match them once they are absent from both the worktree and index.
        indexed = set(self._call(["ls-files", "--cached", "-z", "--", *paths]).split("\0"))
        stage = [path for path in paths if path in indexed or (self.root / path).exists()
                 or (self.root / path).is_symlink()]
        if stage:
            self._call(["add", "-f", "--", *stage])
        self._exclude_artifacts()
        self._call([*identity, "commit", "--only", "-m", title, "--", *paths, f":!{REVIEW_SCOPE_FILE}"])

    @contextmanager
    def publish_lock(self) -> Iterator[None]:
        """Serialize base-branch publication across processes sharing this repository."""
        path = Path(self._call(["rev-parse", "--git-common-dir"]).strip())
        path = (path if path.is_absolute() else self.root / path) / "foragent-publish.lock"
        with path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def update_base(self, branch: str) -> None:
        """Fetch the remote base and fast-forward to it, keeping unrelated local edits."""
        self.fetch("origin", branch)
        remote = f"refs/remotes/origin/{branch}"
        if self.is_ancestor(remote, "HEAD"):
            return
        try:
            self._call(["merge", "--ff-only", "--no-edit", remote])
        except GitError as exc:
            raise GitError(f"cannot fast-forward {branch} to origin/{branch}; "
                           f"local commits or edits conflict with the latest {branch}: {exc}") from exc

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

    def tree_files(self, ref: str) -> list[str]:
        """Every file path tracked at ``ref``."""
        return [path for path in self._call(["ls-tree", "-r", "--name-only", "-z", ref]).split("\0") if path]

    def file_at(self, ref: str, path: str) -> str | None:
        """A tracked file's text at ``ref``, or None when it is missing or not text."""
        try:
            return self._call(["show", f"{ref}:{path}"])
        except (GitError, UnicodeDecodeError):
            return None

    def blob_shas(self, ref: str, paths: Iterable[str]) -> dict[str, str]:
        return {path: self.blob_sha(ref, path) or "missing" for path in paths}

    def remote_slug(self, remote: str = "origin") -> str:
        slug = slug_from_url(self._call(["remote", "get-url", remote]))
        if slug is None:
            raise GitError(f"cannot determine owner/name from remote {remote}; set repo in .foragent.yml")
        return slug


def slug_from_url(url: str) -> str | None:
    """``owner/name`` of a GitHub remote url, else None. The one url-to-slug rule."""
    match = re.fullmatch(
        r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)"
        r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", url.strip(),
    )
    return None if match is None else f"{match[1]}/{match[2]}"


def _common_git_dir(root: Path) -> Path:
    """The git dir holding ``config``; a linked worktree's ``.git`` file names it via ``commondir``."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    line = dot_git.read_text(encoding="utf-8").strip()
    if not line.startswith("gitdir:"):
        raise OSError(f"not a git dir file: {dot_git}")
    gitdir = (root / line.removeprefix("gitdir:").strip()).resolve()
    commondir = gitdir / "commondir"
    if commondir.is_file():
        return (gitdir / commondir.read_text(encoding="utf-8").strip()).resolve()
    return gitdir


def origin_slug_from_files(root: Path, remote: str = "origin") -> str | None:
    """Like ``GitRepo.remote_slug`` but reads git's config file instead of running git.

    None when the remote is missing, not GitHub, or the files cannot be read.
    """
    try:
        text = (_common_git_dir(root) / "config").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    wanted = f'remote "{remote}"'
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            header = line[1:line.find("]")] if "]" in line else ""
            name, _, sub = header.partition(" ")
            section = f"{name.strip().lower()} {sub.strip()}".strip()
            continue
        key, sep, value = line.partition("=")
        if sep and section == wanted and key.strip().lower() == "url":
            return slug_from_url(value.strip().strip('"'))
    return None
