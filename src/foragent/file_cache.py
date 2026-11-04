"""Process-local JSONL reads, shared across stores and invalidated by file version.

Cached values are read-only. Readers exposing mutable records must detach them.
The open descriptor supplies the version, so a replace between stat and open
cannot cache new bytes under the old inode. An in-flight append is retried on
the next read because its size or timestamps differ from the version before reading.
"""

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


_READS: dict[tuple[Path, bool, str], tuple[tuple[int, int, int, int, int], list[Any]]] = {}
_LOCK = Lock()


def _version(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def invalidate(path: Path) -> None:
    path = path.resolve()
    with _LOCK:
        for key in [key for key in _READS if key[0] == path]:
            del _READS[key]


def read_jsonl(path: Path, *, tolerant: bool = False, errors: str = "strict") -> list[Any]:
    """Read once per (device, inode, size, mtime_ns, ctime_ns); missing files are empty.

    Tolerant journals skip malformed lines and non-object records. Strict
    histories retain their existing parse errors. Other I/O errors propagate.
    """
    key = (path.resolve(), tolerant, errors)
    with _LOCK:
        try:
            version = _version(path.stat())
            cached = _READS.get(key)
            if cached is not None and cached[0] == version:
                return cached[1]
            with path.open(encoding="utf-8", errors=errors) as stream:
                version = _version(os.fstat(stream.fileno()))
                rows = []
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        if not tolerant:
                            raise
                        continue
                    if not tolerant or isinstance(row, dict):
                        rows.append(row)
        except FileNotFoundError:
            _READS.pop(key, None)
            return []
        _READS[key] = (version, rows)
        return rows
