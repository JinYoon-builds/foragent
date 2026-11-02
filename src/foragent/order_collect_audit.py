"""Files placed beside the collection copy that report what the run really loaded (#1523).

Python itself tells: an audit hook sees every file opened and every file compiled, started as
early as a launcher allows, so code loaded quietly and forgotten again is still seen.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_MODULE = """import json, os, sys

real = os.path.realpath
CLEAN = [real(os.environ["FORAGENT_COLLECT_TREE"])]
PYTHON = [real(p) for p in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix)]
CALLER = [real(os.environ["FORAGENT_COLLECT_CALLER"])]
CODE = (".py", ".pyc", ".so", ".pyd")
LAUNCHER = real(sys.argv[0]) if sys.argv and sys.argv[0] else ""  # The project chose it; it runs before anything.
seen = set()


def inside(path, roots):
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def outside(path):
    path = real(path)
    return path != LAUNCHER and not inside(path, CLEAN) and (inside(path, CALLER) or not inside(path, PYTHON))


def watch(event, args):
    if event == "open" and isinstance(args[0], (str, os.PathLike)):
        path = os.path.abspath(os.fsdecode(args[0]))
        # Nothing in the caller's checkout belongs to the tree main receives, whatever its type.
        if (path.endswith(CODE) or inside(real(path), CALLER)) and outside(path):
            seen.add(path)
    elif event == "compile" and isinstance(args[1], (str, bytes)):
        path = os.fsdecode(args[1])
        if not path.startswith("<") and outside(os.path.abspath(path)):
            seen.add(os.path.abspath(path))


sys.addaudithook(watch)


def report(destination):
    found = set(seen)
    for name, module in list(sys.modules.items()):
        if name == "__main__":  # The launcher itself: the project chose it.
            continue
        file = getattr(module, "__file__", None)
        for path in ([file] if isinstance(file, str) else []) + [str(p) for p in getattr(module, "__path__", [])]:
            if os.path.isabs(path) and outside(path):
                found.add(path)
    with open(destination, "w", encoding="utf-8") as stream:
        json.dump(sorted(found), stream)
"""

_CONFTEST = """import os, sys

import pytest

sys.path.append(os.path.join(os.path.dirname(__file__), "site"))
import foragent_collect_audit  # Already running when the launcher honoured PYTHONPATH.


class Published(pytest.File):
    def collect(self):
        return []


class AnyPublishedFile:
    # A named file no collector knows has nothing to fail on; pytest would stop the whole run.
    def pytest_collect_file(self, file_path, parent):
        if file_path.suffix != ".py" and parent.session.isinitpath(file_path):
            return Published.from_parent(parent, path=file_path)


def pytest_configure(config):
    config.pluginmanager.register(AnyPublishedFile(), "foragent-collect-published")


def pytest_sessionfinish(session):
    foragent_collect_audit.report(os.environ["FORAGENT_COLLECT_AUDIT"])
"""


def install(guard: Path) -> Path:
    """Write the audit under ``guard``, inside the copy; return the folder Python should load first."""
    site = guard / "site"
    site.mkdir(parents=True)
    (guard / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (site / "foragent_collect_audit.py").write_text(_MODULE, encoding="utf-8")
    (site / "sitecustomize.py").write_text("import foragent_collect_audit\n", encoding="utf-8")
    return site


def started_first(pytest: str, site: Path) -> str:
    """The shell text that starts ``pytest`` with the audit importable from interpreter startup."""
    quoted = shlex.quote(str(site))
    # Its own assignment replaces the environment, so the audit folder joins it there.
    joined, count = re.subn(r"^((?:\w+=\S*\s+)*?)PYTHONPATH=(\S+)", rf"\1PYTHONPATH=\2:{quoted}", pytest, count=1)
    return joined if count else f"PYTHONPATH={quoted} {pytest}"
