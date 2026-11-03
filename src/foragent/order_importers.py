"""Which test files on the base import the test files an order changes (#1774).

Importers are found by a static scan for literal import statements that name a changed module:
``import X``, ``import a.X``, ``from X import ...``, ``from a.X import ...`` and ``from a import X``.
A changed ``__init__.py`` is named by its package, a changed ``conftest.py`` by ``conftest``.
Imports whose module name is computed at run time and relative imports of a package's own names
(``from . import VALUE``) are not goals; tests that reach a changed file through another module are
out of scope.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import PurePosixPath
import subprocess

from foragent.git import GitError, GitRepo


def changed(git: GitRepo, base: str, selected: dict[str, str | bytes]) -> list[str]:
    """This order's Python acceptance files whose bytes differ from the base, new ones included."""
    found = []
    for name, content in selected.items():
        if not name.endswith(".py"):
            continue
        data = content.encode("utf-8") if isinstance(content, str) else content
        current = git.blob_sha(base, name)
        digest = hashlib.sha256 if current is not None and len(current) == 64 else hashlib.sha1
        if current != digest(b"blob %d\0" % len(data) + data).hexdigest():
            found.append(name)
    return found


def module_names(paths: list[str]) -> set[str]:
    """The name each changed file is imported by: its stem, or its package's name for ``__init__.py``."""
    names = set()
    for name in paths:
        path = PurePosixPath(name)
        if path.stem != "__init__":
            names.add(path.stem)
        elif path.parent.name:
            names.add(path.parent.name)
    return names


def imports(source: str, names: set[str]) -> bool:
    """Whether a literal import statement in ``source`` names one of ``names``.

    Any part of a dotted name counts: importing ``a.X`` or ``X.b`` runs ``X`` too. A file that does
    not parse counts as importing, so the check runs it rather than missing it.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dotted = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # `from a import X` imports the module a.X when X is one; its parts include a's.
            dotted = [f"{node.module}.{alias.name}" if node.module else alias.name for alias in node.names]
        else:
            continue
        if any(part in names for name in dotted for part in name.split(".")):
            return True
    return False


def importers(git: GitRepo, base: str, paths: list[str], own: set[str]) -> list[str]:
    """Python files on the base with a literal import of a changed module; never the order's own files.

    ``conftest.py`` files are plugins, not test files, so a conftest that imports a changed module is
    an indirect path and is not run.
    """
    names = module_names(paths)
    if not names:
        return []
    words = [argument for name in sorted(names) for argument in ("-e", name)]
    try:
        # A word search narrows the files to parse. -z prints each path raw, never quoted.
        output = git._call(["grep", "-z", "-l", "-w", "-F", *words, base, "--", "*.py"])
    except GitError as exc:
        cause = exc.__cause__
        if isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 1:
            return []  # No file mentions a changed module.
        raise
    found = []
    for entry in output.split("\0"):
        name = entry[len(base) + 1:] if entry.startswith(f"{base}:") else entry
        if not name or name in own or PurePosixPath(name).name == "conftest.py":
            continue
        if imports(git._call(["show", f"{base}:{name}"]), names):
            found.append(name)
    return sorted(found)
