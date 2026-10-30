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


def resume_selection(cache: StateStore, key: str, selection: str) -> tuple[set[str], str]:
    """The kept passes and ``selection`` deselecting them.

    ``--deselect`` matches node-id prefixes, so a kept id that prefixes another kept id
    (test_work and test_work_more) runs again rather than dropping the other; a resumed
    capture whose pytest reports more deselected tests than it asked for is not complete
    (see ``deselected_matches``).
    """
    retained = retained_passes(cache, key)
    safe = sorted(node for node in retained if not any(other != node and other.startswith(node) for other in retained))
    return retained, selection + "".join(f" {shlex.quote('--deselect=' + node)}" for node in safe)


def deselect_count(command: str) -> int:
    return sum(word.startswith("--deselect=") for word in shlex.split(command))


def after_capture(cache: StateStore, key: str, retained: set[str], result: CheckResult, complete: bool,
                  report: Path, log: Path, offset: int) -> None:
    """A complete capture drops the kept passes; one the watchdog stopped keeps every pass it
    reported, beside those kept before."""
    if complete:
        retained_path(cache, key).unlink(missing_ok=True)
    elif result.timed_out:
        passes = retained | reported_passes(report, log, offset)
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
