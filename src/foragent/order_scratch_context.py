"""The one builder of the lane context a scratch tree receives (#1047).

Entries keep their kind so the scratch matches the source exactly:
``("file", mode, bytes)`` keeps the permission bits (an executable script stays executable),
``("link", target)`` recreates a symlink whose target stays inside the repository (relative,
so it points inside the scratch), and ``None`` removes main's copy of a place first.
A link that resolves outside the repository, a link cycle and any non-regular entry (FIFO,
socket, device) are refused: their repository paths are returned, never silently dropped.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import stat

from foragent.order_scratch import real

Entry = tuple | None


def collect(source: Path, dest: str, base: Path, entries: dict[str, Entry], refused: list[str]) -> None:
    """Add ``source`` (file, link or directory) to ``entries`` at repository path ``dest``.

    ``base`` is where links may point and ``source`` lies inside it: the repository for checkout
    context (placed at the same paths), or a library skill folder placed at ``dest``. A link
    target is mapped from ``base`` to where ``base`` lands in the scratch, so the recreated link
    points to the same file there; its target is collected too (unless it is one of the link's
    own folders). Every folder between ``base`` and ``source`` must stay inside ``base``: a
    linked ancestor that leads elsewhere refuses the whole place.
    """
    root = real(base)
    relative = PurePosixPath(source.relative_to(base).as_posix()) if source != base else PurePosixPath(".")
    landing = PurePosixPath(dest)
    for _ in relative.parts:
        landing = landing.parent
    parent = real(source.parent) if len(relative.parts) > 1 else root
    if root is None or parent is None or not parent.is_relative_to(root):
        refused.append(dest)  # the base, or a linked folder above the place, leads elsewhere
        return
    _visit(source, PurePosixPath(dest), root, landing, entries, refused, set())


def _visit(source: Path, dest: PurePosixPath, root: Path, landing: PurePosixPath, entries: dict,
           refused: list[str], done: set[str]) -> None:
    name = dest.as_posix()
    if name in done:
        return
    done.add(name)
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return
    except OSError:
        refused.append(name)
        return
    try:
        if stat.S_ISREG(info.st_mode):
            entries[name] = ("file", stat.S_IMODE(info.st_mode), source.read_bytes())
            return
        if stat.S_ISDIR(info.st_mode):
            children = sorted(os.listdir(source))
    except OSError:
        refused.append(name)  # unreadable for readers as well; never copied partially
        return
    if stat.S_ISDIR(info.st_mode):
        for child in children:
            _visit(source / child, dest / child, root, landing, entries, refused, done)
    elif stat.S_ISLNK(info.st_mode):
        target = real(source)
        if target is None or not target.is_relative_to(root):
            refused.append(name)  # a cycle, or a link leading outside its base
            return
        inside = PurePosixPath(target.relative_to(root).as_posix())
        placed = landing / inside  # where the target lands in the scratch
        entries[name] = ("link", os.path.relpath(placed.as_posix(), dest.parent.as_posix() or "."))
        if placed not in dest.parents and placed != landing:
            # The target place mirrors the source too: main's copy goes first, so files the
            # source no longer has (or a missing target) do not survive under the link.
            entries.setdefault(placed.as_posix(), None)
            _visit(root / inside, placed, root, landing, entries, refused, done)
    else:
        refused.append(name)  # FIFO, socket or device: not context a reader can use


def place(root: Path, name: str, entries: dict[str, Entry], refused: list[str]) -> None:
    """A checkout context place: main's copy is removed first, then the checkout's entries."""
    entries.setdefault(name, None)
    collect(root / name, name, root, entries, refused)
