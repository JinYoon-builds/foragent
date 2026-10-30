"""Scratch trees for `fa order prepare`: a copy of current main a test writer may change.

The tree is exported from a commit, so the writer never shares the original checkout,
its index or its Git directory. Afterwards the tree is compared with what was put in:
only new regular files under the acceptance directory are candidate tests; every other
difference is reported by path so it can become a question, never silently dropped.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess

from foragent.git import GitError, GitRepo

# Files test runners and build tools discover by directory, so a new one under the acceptance
# directory changes how existing tests there run: shared configuration, never an issue's test.
# Beyond these names, `<x>.config.<ext>` / `<x>.conf.<ext>` files, any dot-named file or
# directory, and any name a tracked file has in a directory above the acceptance directory
# (the repository's own configuration files) count as well.
SHARED_TEST_CONFIG = frozenset({
    "conftest.py", "__init__.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini",
    "noxfile.py", "package.json", "tsconfig.json", "jsconfig.json", "deno.json", "deno.jsonc",
    "bunfig.toml", "cargo.toml", "go.mod", "go.work", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "gemfile", "rakefile", "composer.json", "phpunit.xml",
    "phpunit.xml.dist", "makefile", "cmakelists.txt", "build", "build.bazel", "workspace", "justfile",
})
_CONFIG_NAME = re.compile(r".+\.(?:config|conf)\.[A-Za-z0-9]+")


def shared_configuration(name: str, acceptance_dir: str, above: set[str], *, directories: bool = True) -> bool:
    """Whether a path inside the acceptance directory is configuration other tests share.

    Without ``directories`` a dot-named folder on the way does not count (tool caches).
    """
    parts = PurePosixPath(name).relative_to(PurePosixPath(acceptance_dir.strip("/"))).parts
    base = parts[-1].lower()
    return (base in SHARED_TEST_CONFIG or base in above or bool(_CONFIG_NAME.fullmatch(base))
            or base.startswith(".") or directories and any(part.startswith(".") for part in parts))


def _names_above(manifest: dict, acceptance_dir: str) -> set[str]:
    """Names of tracked files in the directories above the acceptance directory, lower-cased."""
    inside = PurePosixPath(acceptance_dir.strip("/"))
    above = {str(parent) for parent in inside.parents} | {"."}
    return {PurePosixPath(name).name.lower() for name in manifest if str(PurePosixPath(name).parent) in above}


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


def _remove_path(path: Path) -> None:
    """Remove a file, link or directory without following any symlink."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def build(git: GitRepo, base: str, tree: Path, overlay: dict[str, bytes | None]) -> dict[str, tuple[str, str]]:
    """Check ``base`` out into the new directory ``tree`` and apply the overlay; return the baseline.

    The files are checked out as a checkout of ``base`` would have them (no export-ignore or
    export-subst rewriting). In the overlay, ``None`` removes that file or directory first (the
    checkout no longer has it) and bytes then write a file: the lane context the writer reads.
    The baseline is read back from the finished tree, so it is exactly what the writer received.
    """
    tree.mkdir(parents=True)
    index = tree.parent / "scratch.index"
    environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
    try:
        for command in (["read-tree", base], ["checkout-index", "--all", "--force", f"--prefix={tree}/"]):
            done = subprocess.run(["git", *command], cwd=git.root, env=environment, capture_output=True, check=False)
            if done.returncode != 0:
                raise GitError(done.stderr.decode("utf-8", "replace") or f"git {command[0]} {base} failed")
    finally:
        index.unlink(missing_ok=True)
    for name in sorted(name for name, data in overlay.items() if data is None):
        _remove_path(tree / name)
    for name, data in sorted((name, data) for name, data in overlay.items() if data is not None):
        for parent in reversed(PurePosixPath(name).parents[:-1]):
            directory = tree / parent
            if directory.is_symlink() or directory.exists() and not directory.is_dir():
                directory.unlink()
            directory.mkdir(exist_ok=True)
        _remove_path(tree / name)
        (tree / name).write_bytes(data)
    return _entries(tree)


def protected(git: GitRepo, root: Path, state: Path) -> tuple[Path, ...]:
    """Places a writer must not reach: the checkout, its (common) Git directory and the state."""
    places = [root, state]
    try:
        places.append(Path(git._call(["rev-parse", "--path-format=absolute", "--git-common-dir"]).strip()))
    except GitError:
        pass
    return tuple(dict.fromkeys(place.resolve() for place in places))


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
    above = _names_above(manifest, acceptance_dir)
    root = tree.resolve()
    candidates, refused, output = {}, [], set()
    for name in changed:
        path = tree / name
        directory = next((str(parent) for parent in reversed(PurePosixPath(name).parents)
                          if str(parent) + "/" in ignored), None)
        # The exemption covers tool output only: anything there that reaches a protected place
        # (by symlink) or is shared test configuration inside the acceptance directory is refused.
        target = path.resolve()
        if name not in manifest and directory is not None and (target.is_relative_to(root) or not any(
                target.is_relative_to(place.resolve()) for place in protected)) and not (
                PurePosixPath(name).is_relative_to(inside) and shared_configuration(name, acceptance_dir, above, directories=False)):
            output.add(directory)
        elif (name in manifest or found.get(name, ("",))[0] == "120000"
                or not PurePosixPath(name).is_relative_to(inside)
                or shared_configuration(name, acceptance_dir, above)
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
