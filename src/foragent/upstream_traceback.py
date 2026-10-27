"""Read Python tracebacks from their structure only, keeping just verified Foragent facts (#419).

Traceback text is free text: messages, user code and paths can look like anything. Frames come
from indented ``File "...", line N, in f`` lines and the exception from the first column-0 line
after them; every reported field is checked against this foragent package's own files.
"""

import builtins
from pathlib import Path
import re
import sys

import foragent

# The whole function token is captured (up to whitespace), so a frame is never verified by a prefix.
_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)(?=\s|$)')
_TYPE = r"(?:[A-Za-z_]\w*\.)*[A-Z]\w*"
_TYPE_INLINE = re.compile(rf"(?<![\w.])({_TYPE})(?::(?=\s|$)|$)")
# Traceback structure: indented frame lines, then the exception line at column 0.
_FRAME_LINE = re.compile(r'^\s+File "([^"]+)", line (\d+), in (\S+)\s*$')
_TYPE_LINE = re.compile(rf"^({_TYPE})(?::(?=\s|$)|$)")
_HEADER = "Traceback (most recent call last):"
_SYNTHETIC_FUNCTIONS = {"<module>", "<lambda>", "<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>"}


def _package_file(path: str) -> Path | None:
    """The file ``path`` names if it really is a .py file of this foragent package, after resolving."""
    package = Path(foragent.__file__).resolve().parent
    try:
        resolved = Path(path).resolve()
        resolved.relative_to(package)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if resolved.suffix == ".py" and resolved.is_file() else None


def _verified_frame(path: str, line: str, function: str) -> tuple[str, int | None, str] | None:
    """A frame rebuilt from this package's own files, as (foragent/<path>, line or None, function).

    Traceback text is free text, so every field reported is checked against the real file:
    the path is the resolved package file's own relative path; the function must be one plain
    name (Python prints ``co_name``, never a dotted name) that the file defines; the line number
    is kept only if the file has that line, since it may come from another installed version.
    Anything else drops the frame, so no unchecked text reaches a report.
    """
    file = _package_file(path)
    if file is None:
        return None
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if function not in _SYNTHETIC_FUNCTIONS and not (
            re.fullmatch(r"[A-Za-z_]\w*", function) and re.search(
                rf"^\s*(?:async\s+def|def|class)\s+{re.escape(function)}\b", source, re.M)):
        return None
    number = int(line)
    lines = source.count("\n") + (0 if source.endswith("\n") else 1)
    package = Path(foragent.__file__).resolve().parent
    return ("/".join(("foragent", *file.relative_to(package).parts)),
            number if 1 <= number <= lines else None, function)


def _known_exception(name: str) -> bool:
    """Whether ``name`` is a real exception class: built-in, a loaded module's, or foragent's own."""
    module, _, cls = name.rpartition(".")
    candidates = [sys.modules.get(module)] if module else [builtins]
    if any(isinstance(getattr(owner, cls, None), type) and issubclass(getattr(owner, cls), BaseException)
           for owner in candidates if owner is not None):
        return True
    if module and not (module == "foragent" or module.startswith("foragent.")):
        return False
    package = Path(foragent.__file__).resolve().parent
    pattern = re.compile(rf"^class\s+{re.escape(cls)}\b", re.M)
    return any(pattern.search(path.read_text(encoding="utf-8")) for path in package.rglob("*.py"))


def parse_traceback(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """The final exception's class name and its verified Foragent frames, read from structure only.

    Python prints frames as indented ``File "...", line N, in f`` lines, then the exception as
    the first line at column 0. Source and caret lines are indented display text and are never
    read. With chained exceptions only the last ``Traceback`` block counts. The class name must
    be a real exception class, so free text at column 0 never becomes the type.
    """
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip().endswith(_HEADER)]
    block = lines[starts[-1] + 1:] if starts else lines
    raw, last = [], -1
    for index, line in enumerate(block):
        found = _FRAME_LINE.match(line)
        if found:
            raw.append(found.groups())
            last = index
    name = None
    line = next((item for item in block[last + 1:] if item.strip() and not item[:1].isspace()), "")
    found = _TYPE_LINE.match(line)
    if found and _known_exception(found.group(1)):
        name = found.group(1).rsplit(".", 1)[-1]
    return name, [frame for frame in (_verified_frame(*item) for item in raw) if frame]


def _log_traceback(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """The last traceback in a log that has verified Foragent frames.

    Issue logs also hold executor output, such as a user project's failing tests; those
    tracebacks have no Foragent frames, so they are skipped rather than reported.
    """
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.rstrip().endswith(_HEADER)]
    for position in reversed(range(len(starts))):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        name, frames = parse_traceback("\n".join(lines[starts[position]:end]))
        if name and frames:
            return name, frames
    return None, []


def _joined_traceback(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """A journal line with its line breaks removed: frames stay readable, the type only if unambiguous."""
    last = None
    for last in _FRAME.finditer(text):
        pass
    tail = text[last.end():] if last else text
    names = {match.group(1).rsplit(".", 1)[-1] for match in _TYPE_INLINE.finditer(tail)
             if _known_exception(match.group(1))}
    frames = [frame for frame in (_verified_frame(*item) for item in _FRAME.findall(text)) if frame]
    return (names.pop() if len(names) == 1 else None), frames
