"""The pytest plugins a baseline capture loads, and resuming a capture the watchdog stopped.

The collection plugin lists exact node ids (#123). The results plugin records each pass
as it finishes, so a capture the silence watchdog stopped (#435) keeps them: a stopped capture keeps the passes it already reported, in ``<key>.partial`` (not
``*.json``: never a baseline or capture history). The next capture of the same key
deselects them and adds them back when it completes. The capture is the only writer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shlex
import xml.etree.ElementTree as ET

from foragent.run_checks import _append, junit_node_id
from foragent.state import StateStore


def with_results_plugin(command: str, directory: Path) -> tuple[str, Path]:
    """``command`` loading a plugin that records each pass as it finishes, and its listing.

    JUnit is written at session end and the terminal may print only dots, so the plugin
    is what a silenced capture can still keep.
    """
    passes, plugins = directory / "passes.jsonl", directory / "plugins"
    plugins.mkdir()
    plugin = f"_foragent_passes_{secrets.token_hex(8)}"
    (plugins / f"{plugin}.py").write_text(RESULTS_PLUGIN.format(listing=str(passes)), encoding="utf-8")
    return _with_python_path(f"{command} -p {plugin}", str(plugins)), passes


def stopped_capture(cache: StateStore, key: str, retained: set[str], report: Path, log: Path, offset: int,
                    passes: Path, worktree: Path) -> None:
    """Keep every pass the stopped capture reported, beside those an earlier one kept, and its collection."""
    reported, collected = _plugin_records(passes, worktree)
    _retain_passes(cache, key, retained | _reported_passes(report, log, offset) | reported, log,
                   collected or _retained(cache, key).get("collected"))


def deselectable(cache: StateStore, key: str, retained: set[str]) -> set[str]:
    """Retained ids safe to pass to ``--deselect``, which matches node-id prefixes.

    An id that prefixes another collected id (test_work and test_work_more) would also
    drop that one, so it runs again instead. Without a recorded collection, the
    retained ids themselves are the only ones known.
    """
    pool = set(_retained(cache, key).get("collected") or ()) | retained
    return {node for node in retained if not any(other != node and other.startswith(node) for other in pool)}


def retained_path(cache: StateStore, key: str) -> Path:
    # Not *.json: a partial capture is never a baseline or capture history.
    return cache.root / f"{key}.partial"


def _retained(cache: StateStore, key: str) -> dict:
    try:
        saved = json.loads(retained_path(cache, key).read_text(encoding="utf-8"))
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def retained_passes(cache: StateStore, key: str) -> set[str]:
    nodes = _retained(cache, key).get("node_ids")
    return {node for node in nodes if isinstance(node, str)} if isinstance(nodes, list) else set()


def _retain_passes(cache: StateStore, key: str, passes: set[str], log: Path, collected=None) -> None:
    if passes:
        value = {"node_ids": sorted(passes), **({"collected": sorted(collected)} if collected else {})}
        cache._atomic_write(retained_path(cache, key), json.dumps(value) + "\n")
        _append(log, f"Acceptance baseline kept {len(passes)} reported passes for the next capture")


def _reported_passes(report: Path, log: Path, offset: int) -> set[str]:
    """Passes a stopped capture already reported: its partial JUnit report and streamed results."""
    passed = set()
    parser = ET.XMLPullParser(events=("end",))
    try:
        parser.feed(report.read_bytes())
        for _, element in parser.read_events():
            if (element.tag == "testcase" and (node := junit_node_id(element)) is not None
                    and not any(element.find(tag) is not None for tag in ("failure", "error", "skipped"))):
                passed.add(node)
    except (OSError, ET.ParseError):
        pass
    try:
        with log.open(encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                # Verbose lines; a parameterized id can contain spaces.
                if match := re.match(r"(\S.*?::.+?) PASSED(?:\s|$)", line):
                    passed.add(match[1])
    except OSError:
        pass
    return passed


RESULTS_PLUGIN = """\
import json

_passed, _root = {{}}, None


def pytest_configure(config):
    global _root
    _root = str(config.rootpath)


def _record(value):
    with open({listing!r}, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({{**value, "rootdir": _root}}) + "\\n")


def pytest_deselected(items):
    _record({{"collected": [item.nodeid for item in items]}})


def pytest_collection_finish(session):
    _record({{"collected": [item.nodeid for item in session.items]}})


def pytest_runtest_logreport(report):
    if report.failed or (report.skipped and report.when == "call"):
        _passed[report.nodeid] = False
    elif report.when == "call" and report.passed:
        _passed.setdefault(report.nodeid, True)
    if report.when == "teardown" and _passed.pop(report.nodeid, False):
        _record({{"nodeid": report.nodeid}})
"""


def _plugin_records(listing: Path, worktree: Path) -> tuple[set[str], set[str]]:
    """Repository-relative (passed, collected) ids the capture's plugin recorded.

    A pass needs setup, call and teardown to pass; collected ids include deselected ones.
    """
    passed, collected = set(), set()
    try:
        lines = listing.read_text(encoding="utf-8").splitlines()
    except OSError:
        return passed, collected
    for line in lines:
        try:
            record = json.loads(line)
            prefix = Path(record["rootdir"]).resolve().relative_to(worktree.resolve()).as_posix()
        except (ValueError, KeyError, TypeError):
            continue  # A line cut by the stop, or a root outside the checkout.

        def name(nodeid: str) -> str:
            return nodeid if prefix == "." else f"{prefix}/{nodeid}"

        if isinstance(record.get("nodeid"), str):
            passed.add(name(record["nodeid"]))
        if isinstance(record.get("collected"), list):
            collected |= {name(nodeid) for nodeid in record["collected"] if isinstance(nodeid, str)}
    return passed, collected


def _plugin_passes(listing: Path, worktree: Path) -> set[str]:
    return _plugin_records(listing, worktree)[0]


# Written into a throwaway checkout of the new base. It prints nothing, so the
# configured command's terminal flags (-q, verbosity_test_cases, -p no:terminal)
# cannot change the listing; the listing path is part of the source, not the environment.
COLLECT_PLUGIN = """\
import json

_ids, _errors = [], 0


def pytest_collectreport(report):
    global _errors
    if report.failed:  # A collection error; an importorskip skip is not one.
        _errors += 1


def pytest_collection_finish(session):
    _ids[:] = [item.nodeid for item in session.items]


def pytest_sessionfinish(session, exitstatus):
    with open({listing!r}, "w", encoding="utf-8") as stream:
        json.dump({{"ids": _ids, "errors": _errors, "rootdir": str(session.config.rootpath)}}, stream)
"""
def _with_python_path(command: str, directory: str) -> str:
    """Give only this subprocess ``directory`` on PYTHONPATH, joined with the command's own.

    Inline assignments win over outer ones, so the directory joins every
    PYTHONPATH assignment before the command and right after an ``env``
    launcher; without one it is added in front. Assignments keep NAME=
    unquoted (a quoted 'NAME=v' is run as a command, exit 127).
    """
    words = shlex.split(command)

    def assignment(word: str) -> bool:
        return re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", word, re.S) is not None

    # Positions of the leading assignments and of those right after each `env`.
    positions: list[int] = []
    index = 0
    while True:
        while index < len(words) and assignment(words[index]):
            positions.append(index)
            index += 1
        if index < len(words) and Path(words[index]).name == "env":
            index += 1
            continue
        break
    joined = False
    for position in positions:
        name, value = words[position].split("=", 1)
        if name == "PYTHONPATH":
            words[position] = "PYTHONPATH=" + os.pathsep.join(filter(None, [directory, value]))
            joined = True
    if not joined:
        words.insert(0, "PYTHONPATH=" + os.pathsep.join(filter(None, [directory, os.environ.get("PYTHONPATH")])))
        positions = [0, *(position + 1 for position in positions)]
    rendered = []
    for position, word in enumerate(words):
        if position in positions:
            name, value = word.split("=", 1)
            rendered.append(f"{name}={shlex.quote(value)}")
        else:
            rendered.append(shlex.quote(word))
    return " ".join(rendered)
