"""The lane context a scratch tree receives, copied as whole trees (#1047).

Each context source (the checkout's agents folder, lane memory folder and selected skill
folders, or a proposed lane's library skill folders) is copied with ``copy_tree``, a faithful
copy with rsync -a semantics: every entry keeps its type, regular files their bytes and
permission bits, folders their permission bits (applied after their children, so a 0700
folder can still be filled), empty folders stay, symlinks are recreated with the same link
text and FIFOs are recreated. Nothing is selected file by file, so the scratch holds what
the source holds. After copying, every copied link must resolve inside the scratch to the
same content its source link resolves to; any other link, and any entry that cannot be
recreated without privilege (socket, device) or read, is refused by path, never dropped.
The write boundary itself is the inspection after the writer (``order_scratch.changes``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from foragent.order_scratch import _own_parents, _remove_path, real


def copy_tree(source: Path, dest: Path, name: str, refused: list[str], links: list[tuple[str, Path]]) -> None:
    """Copy ``source`` to the new path ``dest`` exactly; ``name`` is its repository path.

    Copied links are appended to ``links`` as (repository path, source link) for ``check_links``.
    """
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return
    except OSError:
        refused.append(name)
        return
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(source), dest)
        if os.chmod in os.supports_follow_symlinks:  # where links have their own modes (macOS)
            os.chmod(dest, mode, follow_symlinks=False)  # the umask must not make it unreadable
        links.append((name, source))
    elif stat.S_ISREG(info.st_mode):
        try:
            data = source.read_bytes()
        except OSError:
            refused.append(name)
            return
        dest.write_bytes(data)
        os.chmod(dest, mode)
    elif stat.S_ISDIR(info.st_mode):
        try:
            children = sorted(os.listdir(source))
        except OSError:
            refused.append(name)
            return
        dest.mkdir()
        os.chmod(dest, 0o700)  # fillable whatever the umask; the source mode follows the children
        for child in children:
            copy_tree(source / child, dest / child, f"{name}/{child}", refused, links)
        os.chmod(dest, mode)  # after the children, so a read-only or 0700 folder keeps its mode
    elif stat.S_ISFIFO(info.st_mode):
        os.mkfifo(dest)
        os.chmod(dest, mode)  # creation modes are masked by the umask; the source mode is set exactly
    else:
        refused.append(name)  # a socket or device cannot be recreated without privilege


def signature(path: Path) -> dict[str, tuple] | None:
    """What a reader finds at ``path`` (following it), entry by entry; None when nothing is there."""
    target = real(path)
    if target is None or not os.path.exists(target):
        return None
    found: dict[str, tuple] = {}

    def visit(entry: Path, relative: str) -> None:
        info = os.lstat(entry)
        if stat.S_ISLNK(info.st_mode):
            found[relative] = ("link", os.readlink(entry))
        elif stat.S_ISREG(info.st_mode):
            found[relative] = ("file", stat.S_IMODE(info.st_mode), entry.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            found[relative] = ("dir", stat.S_IMODE(info.st_mode))
            for child in sorted(os.listdir(entry)):
                visit(entry / child, f"{relative}/{child}")
        else:
            found[relative] = ("special", stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode))

    try:
        visit(target, ".")
    except OSError:
        return {".": ("unreadable",)}
    return found


@dataclass
class Context:
    """The context sources of one issue's scratch: (source tree or None, repository path) pairs.

    A None source (or one the checkout no longer has) only removes main's copy of that path.
    ``files`` are generated files (a proposed lane's agent definition, design references).
    """

    root: Path
    sources: list[tuple[Path | None, str]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)

    def add_checkout(self, name: str) -> None:
        """A checkout context folder or file; it must lie inside the repository, links on the way included."""
        source, base = self.root / name, real(self.root)
        target = real(source.parent)
        if base is None or target is None or not target.is_relative_to(base):
            self.refused.append(name)  # a linked folder above the place leads outside the repository
            self.sources.append((None, name))
        else:
            self.sources.append((source, name))

    def apply(self, tree: Path) -> None:
        """Replace main's copy of each place with a faithful copy of its source, then check links."""
        for _source, name in self.sources:
            if _own_parents(tree, name, create=False):
                _remove_path(tree / name)
        links: list[tuple[str, Path]] = []
        for source, name in self.sources:
            if source is not None and os.path.lexists(source) and not os.path.lexists(tree / name):
                _own_parents(tree, name, create=True)
                copy_tree(source, tree / name, name, self.refused, links)
        for name, data in sorted(self.files.items()):
            _own_parents(tree, name, create=True)
            _remove_path(tree / name)
            (tree / name).write_bytes(data)
            os.chmod(tree / name, 0o644)
        inside = real(tree)
        for name, source in links:
            target = real(tree / name)
            escaped = target is None or inside is None or not target.is_relative_to(inside)
            if escaped or signature(tree / name) != signature(source):
                self.refused.append(name)  # leads outside, or finds other content than its source
        self.refused[:] = sorted(set(self.refused))

