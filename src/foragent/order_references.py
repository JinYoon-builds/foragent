"""Approved design reference files that travel with an order (#981).

Plan JSON may carry reference_files keyed by normalized paths under docs/design/.
Text values are strings; png/jpg/gif values are {"path", "base64"} objects. They are
published in the order's single commit, linked from issue bodies and given to preread.
"""

import base64
import binascii
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from foragent.paths import repo_path

NAMESPACE = "docs/design/"
LIMIT = 1024 * 1024
IMAGE_TYPES = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".gif": "gif"}
SNAPSHOTS = ".foragent/preread-references"


def _error(message: str):
    from foragent.order import PlanError

    return PlanError(message)


def _check_path(root: Path, name) -> None:
    parts = name.split("/") if isinstance(name, str) else []
    if (not isinstance(name, str) or not name.startswith(NAMESPACE) or name.splitlines() != [name]
            or "\\" in name or "\0" in name or len(parts) < 3
            or any(part in ("", ".", "..") for part in parts)):
        raise _error(f"reference_files path must be a normalized repository path under {NAMESPACE}: {name}")
    try:
        path = repo_path(root, name)
    except ValueError as exc:
        raise _error(f"reference_files path must stay under {NAMESPACE}: {name}: {exc}") from exc
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise _error(f"reference_files path must stay under {NAMESPACE} without symlinks: {name}")
    if not path.resolve().is_relative_to(root.resolve() / "docs" / "design"):
        raise _error(f"reference_files path must stay under {NAMESPACE} without symlinks: {name}")


def decode(name: str, value) -> str | bytes:
    """Text as str, images as decoded bytes; reject other shapes and oversized files."""
    label = f"reference_files[{name}]"
    extension = PurePosixPath(name).suffix.lower()
    if isinstance(value, str):
        if extension in IMAGE_TYPES:
            raise _error(f"{label}: {extension} images must be an object with exactly path and base64")
        content, size = value, len(value.encode("utf-8"))
    elif isinstance(value, dict):
        if set(value) != {"path", "base64"}:
            raise _error(f"{label} must be a text string or an object with exactly path and base64")
        if value["path"] != name:
            raise _error(f"{label}.path must equal its reference_files key")
        if extension not in IMAGE_TYPES:
            raise _error(f"{label}: base64 reference objects are only for png, jpg (jpeg) or gif images")
        if not isinstance(value["base64"], str):
            raise _error(f"{label}.base64 must be a base64 string")
        try:
            content = base64.b64decode(value["base64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _error(f"{label}.base64 is not valid strict base64") from exc
        size = len(content)
    else:
        raise _error(f"{label} must be a text string or an object with exactly path and base64")
    if size > LIMIT:
        raise _error(f"reference file {name} is {size} bytes; the limit is {LIMIT} bytes (1 MiB)")
    return content


def validate(plan: dict, root: Path) -> dict[str, str | bytes]:
    references = plan.get("reference_files", {})
    if not isinstance(references, dict):
        raise _error("plan.reference_files must be an object")
    files = {}
    for name, value in references.items():
        _check_path(root, name)
        files[name] = decode(name, value)
    return files


def validate_selection(plan: dict, item: dict, label: str) -> None:
    from foragent.order import _strings

    known = plan.get("reference_files", {})
    for name in _strings(item.get("references", []), f"{label}.references"):
        if name not in known:
            raise _error(f"{label}.references: missing from reference_files: {name}")


def selected(plan: dict, item: dict) -> list[str]:
    """The issue's explicit references, or every plan reference when omitted ([] means none)."""
    chosen = item.get("references")
    return sorted(plan.get("reference_files", {})) if chosen is None else sorted(set(chosen))


LABEL_SPECIAL = set("\\[]<>`*~&")


def markdown_label(text: str) -> str:
    """A file name as link text that renders literally and cannot end or break the link.

    Backslash-escape characters that close the label, start code spans, HTML or
    entities, or add emphasis. An underscore is escaped unless it sits inside a word,
    where GitHub never treats it as emphasis, so common names stay unchanged.
    """
    escaped = []
    for index, char in enumerate(text):
        inside_word = (char == "_" and 0 < index < len(text) - 1
                       and text[index - 1].isalnum() and text[index + 1].isalnum())
        escaped.append(f"\\{char}" if char in LABEL_SPECIAL or (char == "_" and not inside_word) else char)
    return "".join(escaped)


def repository_link(name: str, repo: str, base: str) -> str:
    return f"[{markdown_label(name)}](https://github.com/{repo}/blob/{quote(base, safe='')}/{quote(name)})"


def body_lines(names: list[str], repo: str, base: str) -> list[str]:
    if not names:
        return []
    return ["", "## References", *(f"- {repository_link(name, repo, base)}" for name in names)]


def _decoded(plan: dict, names: list[str]) -> dict[str, str | bytes]:
    return {name: decode(name, plan["reference_files"][name]) for name in names}


def snapshot_dir(root: Path, contents: dict[str, str | bytes]) -> Path:
    digest = hashlib.sha256(json.dumps(
        {name: hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()
         for name, content in contents.items()}, sort_keys=True).encode("utf-8")).hexdigest()
    return root / SNAPSHOTS / digest


def preread_section(plan: dict, item: dict, root: Path) -> str:
    """Selected text inline and absolute paths of the images materialized for preread."""
    contents = _decoded(plan, selected(plan, item))
    if not contents:
        return ""
    snapshot = snapshot_dir(root, contents)
    parts = ["## References (approved design files)"]
    for name, content in contents.items():
        if isinstance(content, bytes):
            parts.append(f"### {name}\nImage; open this file with your image viewer:\n{snapshot / name}\n")
        else:
            fence = "`" * max(3, _longest_backticks(content) + 1)
            parts.append(f"### {name}\n{fence}\n{content}\n{fence}\n")
    return "\n".join(parts) + "\n"


def _longest_backticks(text: str) -> int:
    longest = current = 0
    for char in text:
        current = current + 1 if char == "`" else 0
        longest = max(longest, current)
    return longest


def real_directory(root: Path, relative: str, *, create: bool = True) -> Path | None:
    """root/relative as real directories only, created one level at a time.

    Every component must be a real directory, never a symlink, so writes and
    deletions below it cannot leave the tree. Without create, a missing one gives None.
    """
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise _error(f"{current} must be a real directory, not a symlink or file")
        if not current.exists():
            if not create:
                return None
            current.mkdir()
    return current


def _write_new(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def materialize(plan: dict, item: dict, root: Path) -> Path | None:
    """Write the issue's selected references into a fresh, ignored content-addressed snapshot."""
    from foragent.order_files import check_destinations

    contents = _decoded(plan, selected(plan, item))
    if not contents:
        return None
    snapshot = snapshot_dir(root, contents)
    relative = snapshot.relative_to(root).as_posix()
    check_destinations(snapshot, contents)
    real_directory(root, SNAPSHOTS)
    # A leftover from an interrupted reader is replaced; rmtree never follows links inside it.
    remove(root, snapshot)
    try:
        real_directory(root, relative)
        for name, content in contents.items():
            parent = real_directory(snapshot, PurePosixPath(name).parent.as_posix())
            _write_new(parent / PurePosixPath(name).name,
                       content.encode("utf-8") if isinstance(content, str) else content)
    except BaseException:
        remove(root, snapshot)
        raise
    return snapshot


def remove(root: Path, snapshot: Path | None) -> None:
    """Delete a snapshot only through real directories, so a link never redirects the deletion."""
    if snapshot is None:
        return
    if real_directory(root, snapshot.relative_to(root).as_posix(), create=False) is not None:
        shutil.rmtree(snapshot)
