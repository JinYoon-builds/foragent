"""Scratch trees for `fa order prepare`: a copy of current main a test writer may change.

The tree is exported from a commit, so the writer never shares the original checkout,
its index or its Git directory. Afterwards the tree is compared with what was put in:
only new regular files under the acceptance directory are candidate tests; every other
difference is reported by path so it can become a question, never silently dropped.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

from foragent.git import GitError, GitRepo

# Test collection configuration: a new one under the acceptance directory changes how
# every existing test there runs, so it is shared configuration, not an issue's own test.
SHARED_TEST_CONFIG = frozenset({"conftest.py", "__init__.py", "pytest.ini"})


def blob(data: bytes) -> str:
    """Git's blob hash, so tree entries and written files compare without Git."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def base_revision(git: GitRepo, branch: str) -> str:
    """The commit of current main: the remote branch after a fetch, else the local branch."""
    try:
        git.fetch("origin", branch)
        return git.ref_sha(f"refs/remotes/origin/{branch}")
    except GitError:
        return git.ref_sha(f"refs/heads/{branch}")


def _manifest(git: GitRepo, base: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for entry in git._call(["ls-tree", "-r", "-z", "--full-tree", base]).split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, sha = metadata.split()
        if kind == "blob":
            entries[name] = (mode, sha)
    return entries


def build(git: GitRepo, base: str, tree: Path, overlay: dict[str, bytes]) -> dict[str, tuple[str, str]]:
    """Export ``base`` into the new directory ``tree`` and add the overlay files.

    The overlay is the lane context the writer reads (agent, memory, skills, references).
    The returned manifest maps each path put into the tree to its (mode, blob).
    """
    archive = subprocess.run(["git", "archive", "--format=tar", base], cwd=git.root,
                             capture_output=True, check=False)
    if archive.returncode != 0:
        raise GitError(archive.stderr.decode("utf-8", "replace") or f"git archive {base} failed")
    tree.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        bundle.extractall(tree, filter="tar")
    manifest = _manifest(git, base)
    for name, data in sorted(overlay.items()):
        target = tree / name
        for parent in reversed(PurePosixPath(name).parents[:-1]):
            directory = tree / parent
            if directory.is_symlink() or directory.exists() and not directory.is_dir():
                directory.unlink()
            directory.mkdir(exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_bytes(data)
        manifest[name] = ("100644", blob(data))
    return manifest


def stamps(tree: Path, names) -> dict[str, tuple[int, int, int]]:
    """Each file's inode, change time and modification time, taken before the writer starts.

    A user cannot set a file's change time back, so a writer that edits a file and
    restores its bytes (or swaps the file) still leaves a different stamp.
    """
    found = {}
    for name in names:
        try:
            info = os.lstat(tree / name)
        except OSError:
            continue
        found[name] = (info.st_ino, info.st_ctime_ns, info.st_mtime_ns)
    return found


def _entries(tree: Path) -> dict[str, tuple[str, str]]:
    """Every file and symlink in the tree, without following any symlink."""
    found = {}
    for directory, folders, files in os.walk(tree, followlinks=False):
        here = Path(directory)
        for name in [*files, *(folder for folder in folders if (here / folder).is_symlink())]:
            path = here / name
            relative = path.relative_to(tree).as_posix()
            if path.is_symlink():
                found[relative] = ("120000", blob(os.fsencode(os.readlink(path))))
            elif path.is_file():
                mode = "100755" if os.access(path, os.X_OK) else "100644"
                found[relative] = (mode, blob(path.read_bytes()))
    return found


def _ignored(git: GitRepo, tree: Path, names: list[str]) -> set[str]:
    """The given paths the repository's ignore rules exclude, as given."""
    if not names:
        return set()
    git_dir = git._call(["rev-parse", "--absolute-git-dir"]).strip()
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", "--work-tree=.", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=tree, input="\0".join(names).encode() + b"\0", capture_output=True, check=False)
    return {name.decode() for name in result.stdout.split(b"\0") if name}


def changes(git: GitRepo, tree: Path, manifest: dict[str, tuple[str, str]], acceptance_dir: str,
            protected: tuple[Path, ...] = (),
            before: dict[str, tuple[int, int, int]] | None = None) -> tuple[dict[str, bytes], list[str], list[str]]:
    """New test files the writer may hand over, every other changed path, and tool output.

    A candidate is a new regular file inside ``acceptance_dir`` whose real path stays in
    the tree. Changes to existing files (tests included), deletions, symlinks, shared test
    configuration and anything outside the directory are refused by path. Ignore rules
    exempt only whole new directories they ignore (caches and environments the writer's
    runs leave), which are listed; an ignored file anywhere else is refused like any other,
    and so is anything in such a directory that resolves into a ``protected`` directory.
    With ``before`` (see ``stamps``) a file whose bytes match but whose stamp differs was
    touched during the run, for example edited and restored, and is refused as changed.
    """
    found = _entries(tree)
    touched = set() if before is None else {
        name for name, stamp in stamps(tree, before).items() if before[name] != stamp}
    changed = sorted(name for name in found.keys() | manifest.keys()
                     if found.get(name) != manifest.get(name) or name in touched)
    existing = {str(parent) for name in manifest for parent in PurePosixPath(name).parents}
    new_directories = sorted({str(parent) for name in changed if name not in manifest
                              for parent in PurePosixPath(name).parents if str(parent) not in existing})
    ignored = _ignored(git, tree, [directory + "/" for directory in new_directories])
    inside = PurePosixPath(acceptance_dir.strip("/"))
    root = tree.resolve()
    candidates, refused, output = {}, [], set()
    for name in changed:
        path = tree / name
        directory = next((str(parent) for parent in reversed(PurePosixPath(name).parents)
                          if str(parent) + "/" in ignored), None)
        if name not in manifest and directory is not None and not any(
                path.resolve().is_relative_to(place.resolve()) for place in protected):
            output.add(directory)
        elif (name in manifest or found.get(name, ("",))[0] == "120000"
                or not PurePosixPath(name).is_relative_to(inside)
                or PurePosixPath(name).name in SHARED_TEST_CONFIG
                or any((tree / parent).is_symlink() for parent in PurePosixPath(name).parents)
                or not path.resolve().is_relative_to(root)):
            refused.append(name)
        else:
            candidates[name] = path.read_bytes()
    unignored = [name for name in candidates if name not in _ignored(git, tree, list(candidates))]
    refused += sorted(set(candidates) - set(unignored))
    return {name: candidates[name] for name in unignored}, sorted(refused), sorted(output)


def remove(tree: Path) -> None:
    """Delete a scratch tree this command made; symlinks inside are removed, not followed."""
    if tree.is_symlink():
        tree.unlink()
    elif tree.exists():
        shutil.rmtree(tree)
