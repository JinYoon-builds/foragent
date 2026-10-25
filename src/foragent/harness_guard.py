"""Find test-harness changes a PR introduces outside its issue's ## Files."""

from __future__ import annotations

from pathlib import PurePosixPath
import tomllib
from typing import TYPE_CHECKING

from foragent.git import GitError
from foragent.policy import touched

if TYPE_CHECKING:
    from foragent.git import GitRepo

HARNESS_NAMES = frozenset({
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "sitecustomize.py", "usercustomize.py",
})
# [tool.pytest] contains [tool.pytest.ini_options] and every other sub-table.
PYPROJECT_TABLES = (("tool", "pytest"), ("project", "entry-points", "pytest11"))
_UNPARSABLE = object()


def _pytest_tables(text: str | None) -> object:
    if text is None:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return _UNPARSABLE
    tables = []
    for keys in PYPROJECT_TABLES:
        value: object = data
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        tables.append(value)
    return tables


def _show(git: GitRepo, spec: str) -> str | None:
    try:
        return git._call(["show", spec])
    except GitError:
        return None  # Absent on that side.


def changed_harness_paths(git: GitRepo, target_ref: str, listed: list[str]) -> list[str]:
    """Harness paths the staged PR contents change since its merge base with the target.

    Renames count as a deletion plus an addition, so both paths are reported.
    """
    base = git._own_base(target_ref)
    found = []
    for path in git.changed_paths(target_ref):
        name = PurePosixPath(path).name
        if name == "pyproject.toml":
            before = _pytest_tables(_show(git, f"{base}:{path}"))
            after = _pytest_tables(_show(git, f":{path}"))
            if before is not _UNPARSABLE and after is not _UNPARSABLE and before == after:
                continue
        elif name not in HARNESS_NAMES:
            continue
        if not touched([path], listed):
            found.append(path)
    return found


def hold_details(paths: list[str]) -> str:
    return "Changed test-harness paths not listed in ## Files:\n" + "\n".join(f"- `{path}`" for path in paths)
