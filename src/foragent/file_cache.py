"""One process-local parsed copy per file version, shared by every state reader.

Values are read-only; public mutable records must be detached by their reader.
Only this module writes the cache. State writers retain their existing locks and
atomic replaces; an open descriptor supplies the identity of the bytes parsed.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Callable


_READS: dict[Path, tuple[tuple[int, int, int, int, int], Any]] = {}
_LOCK = Lock()


@dataclass(slots=True)
class _Failure:
    error: Exception


@dataclass(slots=True)
class Line:
    offset: int
    value: Any
    blank: bool
    unicode_error: UnicodeDecodeError | None = None

    def parsed(self) -> Any:
        if isinstance(self.value, _Failure):
            raise self.value.error.with_traceback(None)
        return self.value


@dataclass(slots=True)
class Journal:
    data: bytes
    lines: list[Line]
    # Views contain references to the one parsed tree, never another parsed copy.
    views: dict[tuple[bool, str], Any] = field(default_factory=dict)

    def rows(self, tolerant: bool, errors: str) -> list[Any]:
        key = tolerant, errors
        if key not in self.views:
            rows = []
            try:
                for line in self.lines:
                    if errors == 'strict' and line.unicode_error is not None:
                        raise line.unicode_error
                    try:
                        row = line.parsed()
                    except ValueError:
                        if not tolerant:
                            raise
                        continue
                    if not tolerant or isinstance(row, dict):
                        rows.append(row)
                self.views[key] = rows
            except Exception as exc:
                self.views[key] = _Failure(exc.with_traceback(None))
        value = self.views[key]
        if isinstance(value, _Failure):
            raise value.error.with_traceback(None)
        return value

    @property
    def size(self) -> int:
        return len(self.data)

    def require_utf8(self):
        for line in self.lines:
            if line.unicode_error is not None:
                raise line.unicode_error.with_traceback(None)
        return self

    def matching(self, needle: bytes):
        """Preserve readers' raw-text selection without parsing selected lines again."""
        for index, line in enumerate(self.lines):
            end = self.lines[index + 1].offset if index + 1 < len(self.lines) else self.size
            if needle in self.data[line.offset:end]:
                yield line

    def tail(self, limit: int) -> tuple[int, list[Line]]:
        """Complete LF-framed lines inside the last limit bytes, as a seek/read would see."""
        start = max(0, self.size - limit)
        return start, [line for line in self.lines if line.offset >= start]


def _version(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def invalidate(path: Path) -> None:
    with _LOCK:
        _READS.pop(path.resolve(), None)


@contextmanager
def temporary_reads(root: Path):
    """Drop parsed temporary snapshots when their owning facts call ends."""
    try:
        yield
    finally:
        root = root.resolve()
        with _LOCK:
            for key in list(_READS):
                if key.is_relative_to(root):
                    del _READS[key]


def read(path: Path, parse: Callable[[bytes], Any]) -> Any:
    """Read/parse once per real path and fstat version, regardless of caller or view.

    A path has one format (JSON or JSONL). ``parse`` supplies that format, never
    a caller-specific filter. Entries live for the process: use this for stable
    state paths, scope temporary snapshots with ``temporary_reads``, and stream
    session transcripts instead. Missing files and other I/O errors propagate.
    """
    key = path.resolve()
    with _LOCK:
        try:
            version = _version(path.stat())
        except FileNotFoundError:
            _READS.pop(key, None)
            raise
        cached = _READS.get(key)
        if cached is not None and cached[0] == version:
            value = cached[1]
        else:
            with path.open('rb') as stream:
                version = _version(os.fstat(stream.fileno()))
                data = stream.read()
            try:
                value = parse(data)
            except Exception as exc:
                value = _Failure(exc.with_traceback(None))
            _READS[key] = (version, value)
        if isinstance(value, _Failure):
            raise value.error.with_traceback(None)
        return value


def read_json(path: Path) -> Any:
    return read(path, lambda data: json.loads(data.decode('utf-8')))


def _parse_lines(data: bytes) -> Journal:
    lines, offset = [], 0
    for raw in data.split(b'\n'):
        # There is no extra line after a terminal newline (nor in an empty file).
        if offset == len(data):
            break
        unicode_error = None
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            unicode_error = exc.with_traceback(None)
            text = raw.decode('utf-8', errors='replace')
        try:
            value = json.loads(text)
        except Exception as exc:
            value = _Failure(exc.with_traceback(None))
        lines.append(Line(offset, value, not raw.strip(), unicode_error))
        offset += len(raw) + 1
    return Journal(data, lines)


def read_lines(path: Path) -> Journal:
    """Parsed values and per-line failures/byte offsets, independent of reader policy."""
    return read(path, _parse_lines)


def read_jsonl(path: Path, *, tolerant: bool = False, errors: str = 'strict') -> list[Any]:
    """Strict histories raise; tolerant journals skip malformed/non-object rows.

    UTF-8 policy is independent of JSON tolerance. Replacement readers share the
    same parsed values as strict readers, including when a strict view must fail.
    """
    try:
        journal = read_lines(path)
    except FileNotFoundError:
        return []
    return journal.rows(tolerant, errors)
