"""Commit and push exactly the tree that was collected, or leave the caller's files as they were (#1523).

Publication holds its own lock, yet another process can still move this checkout. Each step
compares the checkout with the collected commit, and the push names the commit it verified.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import tempfile

from foragent.config import Config
from foragent.git import GitRepo
from foragent.i18n import text
from foragent.paths import repo_path


def push(git: GitRepo, branch: str, sha: str, exact: bool) -> None:
    """Push the verified commit by name: a branch that advanced since then stays unpublished."""
    if exact:
        git._call(["push", "origin", f"{sha}:refs/heads/{branch}"])
    else:
        git.push_base(branch)


class Landing:
    """One publication's write: ``open``, write files, ``commit``, ``finish``. No check without a commit."""

    def __init__(self, git: GitRepo, root: Path, checked: str | None, config: Config,
                 files: dict[str, str | bytes] | None = None, extra: Iterable[str] = ()) -> None:
        self.git, self.root, self.checked, self.config = git, root, checked, config
        self.files, self.extra = files or {}, set(extra)
        self.saved: dict[str, bytes | None] = {}
        self.index: dict[str, str] = {}
        self.ours: str | None = None

    def open(self) -> Landing:
        """Refuse a moved checkout before anything is written, and remember what the write replaces."""
        if self.checked is None:
            return self
        self._require_unmoved(())
        for name in self.files:
            path = repo_path(self.root, name)
            self.saved[name] = path.read_bytes() if path.is_file() else None
            entry = self.git._call(["ls-files", "--stage", "--", name]).strip()
            if entry:
                self.index[name] = entry
        return self

    def commit(self, paths: list[str], title: str) -> None:
        if self.checked is None:
            self.git.commit_paths(paths, title)
            return
        before = self.git.head_sha()
        self.git.commit_paths(paths, title)
        after = self.git.head_sha()
        self.ours = after if after != before else None

    def finish(self) -> str:
        """The commit to push, verified to be the collected tree plus this publication's exact bytes."""
        sha = self.git.head_sha()
        if self.checked is None:
            return sha
        try:
            self._require_unmoved(self.extra | set(self.files), sha)
            changed = [name for name in self.files if self.git.blob_sha(sha, name) != self._blob(name)]
            if (self.ours is not None and sha != self.ours) or changed:
                raise self._moved(changed or [sha[:12]])
        except ValueError:
            self._undo(sha)
            raise
        return sha

    def _blob(self, name: str) -> str:
        content = self.files[name]
        data = content.encode("utf-8") if isinstance(content, str) else content
        with tempfile.NamedTemporaryFile() as stream:  # Git applies the path's own filters to these bytes.
            stream.write(data)
            stream.flush()
            return self.git._call(["hash-object", f"--path={name}", stream.name]).strip()

    def _moved(self, paths: Iterable[str]):
        from foragent.order import PlanError

        language = (getattr(self.config, "language", None) or "en").lower().split("-")[0]
        return PlanError(text("order_collect_base_moved", language, base=self.checked[:12],
                              paths=", ".join(sorted(paths))))

    def _require_unmoved(self, own: Iterable[str], head: str = "HEAD") -> None:
        changed = set(self.git._call(["diff", "--name-only", "--no-renames", self.checked, head]).splitlines())
        moved = changed - set(own)
        if moved:
            raise self._moved(moved)

    def _undo(self, sha: str) -> None:
        """Take back this publication's own commit. Commits another process added on top stay theirs."""
        if self.ours != sha:
            return
        parent = self.git._call(["rev-parse", f"{sha}^"]).strip()
        self.git._call(["update-ref", "HEAD", parent, sha])
        self._restore()

    def _restore(self) -> None:
        for name, data in self.saved.items():
            if name in self.index:
                mode, blob, _stage = self.index[name].split("\t", 1)[0].split()
                self.git._call(["update-index", "--add", "--cacheinfo", f"{mode},{blob},{name}"])
            else:
                self.git._call(["update-index", "--force-remove", "--", name])
            path = repo_path(self.root, name)
            if data is not None:
                path.write_bytes(data)
            elif path.is_file():
                path.unlink()
                for parent in PurePosixPath(name).parents:
                    if str(parent) == "." or any((self.root / parent).iterdir()):
                        break
                    (self.root / parent).rmdir()
