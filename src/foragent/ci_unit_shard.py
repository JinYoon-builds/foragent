"""Split the unit test files into CI shards and run one shard with pytest."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def select(paths: list[str], shard: int, shards: int) -> list[str]:
    """Return this shard's files: sorted round-robin, so shards are disjoint and stable."""
    if shards < 1 or not 0 <= shard < shards:
        raise ValueError(f"shard must be in 0..{shards - 1}, got {shard} of {shards}")
    return sorted(paths)[shard::shards]


def unit_files(root: Path) -> list[str]:
    """Unit test files: tests/**/test_*.py outside tests/acceptance."""
    tests = root / "tests"
    acceptance = tests / "acceptance"
    return sorted(
        path.relative_to(root).as_posix()
        for path in tests.rglob("test_*.py")
        if path.is_file() and not path.is_relative_to(acceptance)
    )


def main() -> int:
    shard = int(os.environ["FA_SHARD"])
    shards = int(os.environ["FA_SHARDS"])
    files = select(unit_files(Path.cwd()), shard, shards)
    if not files:
        print(f"shard {shard}/{shards}: no unit test files")
        return 0
    command = [sys.executable, "-m", "pytest", "-q", "-m", "not local_only", *files]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
