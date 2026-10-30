"""Resume a baseline capture the silence watchdog stopped (#435).

A stopped capture keeps the passes it already reported in ``<key>.partial`` (not
``*.json``: never a baseline or capture history). The next capture of the same key
deselects them and adds them back when it completes. The capture is the only writer.
Kept apart from run_baseline only for its module-size limit.
"""

from __future__ import annotations

import inspect
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


# Part of every D53 baseline and .partial cache key: ids before #435's shared identity are not reused.
CACHE_VERSION = "435-ids-2"


def test_id(nodeid, rootdir, worktree):
    """The one test identity: ``nodeid`` relative to the worktree root.

    realpath applies to the two roots only (so /var and /private/var match), never to the
    joined test path; an id whose rootdir lies outside the worktree stays the raw nodeid.
    This body is copied into the generated plugin, which cannot import foragent.
    """
    import os

    prefix = os.path.relpath(os.path.realpath(str(rootdir)), os.path.realpath(str(worktree))).replace(os.sep, "/")
    if prefix == "." or prefix == ".." or prefix.startswith("../"):
        return nodeid
    return prefix + "/" + nodeid


def retained_path(cache: StateStore, key: str) -> Path:
    return cache.root / f"{key}.partial"


def _retained(cache: StateStore, key: str) -> tuple[set[str], set[str]]:
    """(kept passes, ids reported as anything else), saved together across stopped captures."""
    try:
        saved = json.loads(retained_path(cache, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set()
    if not isinstance(saved, dict):
        return set(), set()

    def ids(name):
        values = saved.get(name)
        return {node for node in values if isinstance(node, str)} if isinstance(values, list) else set()

    failed = ids("failed")
    return ids("node_ids") - failed, failed


def retained_passes(cache: StateStore, key: str) -> set[str]:
    return _retained(cache, key)[0]


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
        listing=str(listing), keep=sorted(retained), repo=str(worktree), test_id=inspect.getsource(test_id)),
        encoding="utf-8")
    return retained, _with_python_path(f"{command} -p {plugin}", str(plugins)), listing


def combine(node_ids, collected_ids, failing_ids, retained: set[str], log: Path, offset: int, worktree: Path):
    """A normal-end capture's passing, collected and failing ids, in the shared identity: its own
    results, plus the kept passes it did not run again (a rerun keeps this capture's result)."""
    rootdir = _rootdir(log, offset, worktree)
    own = [tuple(test_id(node, rootdir, worktree) for node in ids) for ids in (node_ids, collected_ids, failing_ids)]
    kept = retained - set(own[1])
    return tuple(sorted({*own[0], *kept})), tuple(sorted({*own[1], *kept})), tuple(sorted(own[2]))


def _rootdir(log: Path, offset: int, worktree: Path):
    """The rootdir pytest printed in its header (``rootdir: ...``), else the worktree root."""
    try:
        with log.open(encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                if match := re.match(r"rootdir: (.+?)(?:, |$)", line.rstrip("\n")):
                    return match[1]
    except OSError:
        pass
    return worktree


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


{test_id}

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
    kept = [item for item in items if test_id(item.nodeid, _root, _repo) in _keep]
    items[:] = [item for item in items if test_id(item.nodeid, _root, _repo) not in _keep]
    if kept:
        config.hook.pytest_deselected(items=kept)
    _record({{"deselected": len(original) - len(items)}})  # What this capture really did not run.


def pytest_runtest_logreport(report):
    # A pass needs setup, call and teardown to pass; failed, error, skipped, xfail or xpass is not one.
    if report.failed or report.skipped or hasattr(report, "wasxfail"):
        if _passed.get(report.nodeid) is not False:
            _record({{"failed": test_id(report.nodeid, _root, _repo)}})
        _passed[report.nodeid] = False
    elif report.when == "call":
        _passed.setdefault(report.nodeid, True)
    if report.when == "teardown" and _passed.pop(report.nodeid, False):
        _record({{"passed": test_id(report.nodeid, _root, _repo)}})
"""


def after_capture(cache: StateStore, key: str, result: CheckResult, complete: bool,
                  report: Path, log: Path, offset: int, listing: Path, worktree: Path) -> None:
    """A normal-end complete capture drops the kept results. Any other capture saves every
    non-pass it reported (a failure always wins over a pass, now and in later captures), and
    one that was stopped (silence, safety net, runtime limit) also keeps the passes it reported."""
    if complete:
        retained_path(cache, key).unlink(missing_ok=True)
        return
    kept, failed = _retained(cache, key)
    passed, now_failed = reported_results(report, log, offset, listing, worktree)
    failed |= now_failed
    passes = (kept | (passed if result.timed_out else set())) - failed
    if passes or failed:
        cache._atomic_write(retained_path(cache, key),
                            json.dumps({"node_ids": sorted(passes), "failed": sorted(failed)}) + "\n")
        _append(log, f"Acceptance baseline kept {len(passes)} reported passes for the next capture")


def capture_failure(result: CheckResult, message: str) -> AttemptFailure:
    """An unusable capture; one the watchdog stopped carries its kind, so the run ends by its outcome."""
    failure = AttemptFailure(message)
    if result.timed_out:
        failure.watchdog_kind = result.stop or "silent"
    return failure


def reported_results(report: Path, log: Path, offset: int, listing: Path, worktree: Path) -> tuple[set[str], set[str]]:
    """(passed, failed) ids from all three sources, in the shared identity; failed wins over passed.

    The plugin's records are already relative to the worktree; JUnit ids are relative to the
    rootdir pytest printed; PASSED lines are relative to the directory pytest ran in, the worktree.
    """
    passed, failed = set(), set()
    for record in _records(listing):
        for outcome, ids in (("passed", passed), ("failed", failed)):
            if isinstance(record.get(outcome), str):
                ids.add(record[outcome])
    rootdir = _rootdir(log, offset, worktree)
    parser = ET.XMLPullParser(events=("end",))
    try:
        parser.feed(report.read_bytes())
        for _, element in parser.read_events():
            if element.tag == "testcase" and (node := junit_node_id(element)) is not None:
                bad = any(element.find(tag) is not None for tag in ("failure", "error", "skipped"))
                (failed if bad else passed).add(test_id(node, rootdir, worktree))
    except (OSError, ET.ParseError):
        pass
    try:
        with log.open(encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                # Verbose lines; a parameterized id can contain spaces.
                if match := re.match(r"(\S.*?::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)", line):
                    (passed if match[2] == "PASSED" else failed).add(test_id(match[1], worktree, worktree))
    except OSError:
        pass
    return passed - failed, failed

