"""Resume a baseline capture the silence watchdog stopped (#435).

A stopped capture keeps the passes it already reported in ``<key>.partial`` (not
``*.json``: never a baseline or capture history). The next capture of the same key
deselects them and adds them back when it completes. The capture is the only writer.
Kept apart from run_baseline only for its module-size limit.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import shlex
import xml.etree.ElementTree as ET

from foragent.run_attempt import AttemptFailure
from foragent.run_checks import CheckResult, _append, junit_node_id
from foragent.state import StateStore


def checkout(git, worktree: Path):
    """The capture checkout's repository; a Git adapter without checkouts has none to inspect."""
    at = getattr(git, "at", None)
    return at(worktree) if at is not None else None


def retained_path(cache: StateStore, key: str) -> Path:
    return cache.root / f"{key}.partial"


def retained_passes(cache: StateStore, key: str) -> set[str]:
    try:
        saved = json.loads(retained_path(cache, key).read_text(encoding="utf-8"))
        nodes = saved["node_ids"] if isinstance(saved, dict) else ()
        return {node for node in nodes if isinstance(node, str)}
    except (OSError, ValueError, TypeError, KeyError):
        return set()


def prepare(cache: StateStore, key: str, command: str, directory: Path, worktree: Path) -> tuple[set[str], str, Path]:
    """The kept passes, ``command`` resuming after them, and the listing its plugin writes.

    ``--deselect`` matches node-id prefixes, so the plugin puts back every test it dropped
    only by prefix (keeping test_work also drops test_work_more): only exactly kept ids stay
    out, also a kept id that prefixes another kept id, which is not passed to --deselect at all.
    """
    from foragent.run_baseline import _with_python_path

    retained = retained_passes(cache, key)
    safe = sorted(node for node in retained if not any(other != node and other.startswith(node) for other in retained))
    command += "".join(f" {shlex.quote('--deselect=' + node)}" for node in safe)
    listing, plugins = directory / "results.jsonl", directory / "results-plugin"
    plugins.mkdir()
    plugin = f"_foragent_results_{secrets.token_hex(8)}"
    (plugins / f"{plugin}.py").write_text(RESULTS_PLUGIN.format(
        listing=str(listing), keep=sorted(retained), repo=str(worktree)), encoding="utf-8")
    return retained, _with_python_path(f"{command} -p {plugin}", str(plugins)), listing


def combine(node_ids, collected_ids, retained: set[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """A resumed capture's passing and collected ids: its own results, plus the kept passes it
    did not run again; a kept id this capture ran again keeps this capture's result."""
    kept = retained - set(collected_ids)
    return tuple(sorted({*node_ids, *kept})), tuple(sorted({*collected_ids, *kept}))


def deselect_count(command: str) -> int:
    return sum(word.startswith("--deselect=") for word in shlex.split(command))


def selected(collected: int, line: str, listing: Path, command: str) -> int:
    """The collected tests the capture ran: less those deselected exactly (the plugin's count),
    or, without it, less the deselected count pytest printed when it matches what was asked."""
    exact = [record["deselected"] for record in _records(listing) if isinstance(record.get("deselected"), int)]
    if exact:
        return collected - exact[-1]
    printed = int((re.search(r"(\d+) deselected", line) or [0, 0])[1])
    return collected - printed if printed == deselect_count(command) else -1  # A prefix match: incomplete.


def _records(listing: Path) -> list[dict]:
    try:
        lines = listing.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue  # A line cut by the stop.
        if isinstance(value, dict):
            records.append(value)
    return records


RESULTS_PLUGIN = """\
import json
import os

import pytest

_keep, _repo, _root, _passed = set({keep!r}), {repo!r}, None, {{}}


def _id(nodeid):
    prefix = os.path.relpath(_root, _repo).replace(os.sep, "/")
    return nodeid if prefix == "." else prefix + "/" + nodeid


def _record(value):
    with open({listing!r}, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\\n")


def pytest_configure(config):
    global _root
    _root = str(config.rootpath)


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(session, config, items):
    original = list(items)
    yield
    prefixes = tuple(config.getoption("deselect") or ())
    present = {{id(item) for item in items}}
    # --deselect matches prefixes: put back what it dropped only by prefix ...
    items.extend(item for item in original if id(item) not in present and prefixes
                 and item.nodeid.startswith(prefixes))
    # ... then leave out exactly the kept ids, also those not passed to --deselect.
    kept = [item for item in items if _id(item.nodeid) in _keep]
    items[:] = [item for item in items if _id(item.nodeid) not in _keep]
    if kept:
        config.hook.pytest_deselected(items=kept)
    _record({{"deselected": len(original) - len(items)}})  # What this capture really did not run.


def pytest_runtest_logreport(report):
    if report.failed or (report.skipped and report.when == "call"):
        _passed[report.nodeid] = False
    elif report.when == "call" and report.passed:
        _passed.setdefault(report.nodeid, True)
    if report.when == "teardown" and _passed.pop(report.nodeid, False):
        _record({{"passed": _id(report.nodeid)}})
"""


def after_capture(cache: StateStore, key: str, retained: set[str], result: CheckResult, complete: bool,
                  report: Path, log: Path, offset: int, listing: Path) -> None:
    """A complete capture drops the kept passes; one the watchdog stopped keeps every pass it
    reported, beside those kept before."""
    if complete:
        retained_path(cache, key).unlink(missing_ok=True)
    elif result.timed_out:
        plugin = {record["passed"] for record in _records(listing) if isinstance(record.get("passed"), str)}
        passes = retained | reported_passes(report, log, offset) | plugin
        if passes:
            cache._atomic_write(retained_path(cache, key), json.dumps({"node_ids": sorted(passes)}) + "\n")
            _append(log, f"Acceptance baseline kept {len(passes)} reported passes for the next capture")


def capture_failure(result: CheckResult, message: str) -> AttemptFailure:
    """An unusable capture; one the watchdog stopped carries its kind, so the run ends by its outcome."""
    failure = AttemptFailure(message)
    if result.timed_out:
        failure.watchdog_kind = result.stop or "silent"
    return failure


def reported_passes(report: Path, log: Path, offset: int) -> set[str]:
    """Ids a stopped capture reported as passed, and nothing else about them failed.

    Results are aggregated per id across every record: a pass followed by a teardown
    error (two JUnit records, or a PASSED then an ERROR line) is not a pass.
    """
    passed, failed = set(), set()
    parser = ET.XMLPullParser(events=("end",))
    try:
        parser.feed(report.read_bytes())
        for _, element in parser.read_events():
            if element.tag == "testcase" and (node := junit_node_id(element)) is not None:
                bad = any(element.find(tag) is not None for tag in ("failure", "error", "skipped"))
                (failed if bad else passed).add(node)
    except (OSError, ET.ParseError):
        pass
    try:
        with log.open(encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                # Verbose lines; a parameterized id can contain spaces.
                if match := re.match(r"(\S.*?::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)", line):
                    (passed if match[2] == "PASSED" else failed).add(match[1])
    except OSError:
        pass
    return passed - failed
