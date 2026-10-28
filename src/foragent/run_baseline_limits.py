"""Pure reads of the machine-wide baseline cache: membership, durations and the capture limit.

The runner computes its capture limit here (and logs it); `fa orch facts` reads the
same history to show each build and the trend against that limit. Nothing here
writes, starts a process or signals one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from math import isfinite
from pathlib import Path
import xml.etree.ElementTree as ET

DEFAULT_LIMIT = 900
MAX_LIMIT = 3600


@dataclass(frozen=True)
class CacheEntry:
    key: str
    saved: dict
    created_at: datetime
    # Recorded duration, or the companion JUnit total for a legacy entry; None when unknown.
    duration_seconds: float | None
    estimated: bool
    timed_out: bool


def belongs(saved: dict, path: Path, repo_root: Path, repo: str | None) -> bool:
    """A repository path decides; without one, the slug; without either, a companion report."""
    if "repository" in saved:
        return saved["repository"] == str(repo_root.resolve())
    if "repo" in saved:
        return repo is not None and saved["repo"] == repo
    # Legacy entries lack tags; accept companion reports in this shared cache.
    return path.with_suffix(".xml").is_file()


def _junit_total(report: Path) -> float | None:
    junit = ET.parse(report).getroot()
    suites = [junit] if junit.tag == "testsuite" else junit.findall("testsuite")
    if junit.tag not in {"testsuite", "testsuites"} or not suites:
        return None
    return sum(float(suite.attrib["time"]) for suite in suites)


def _estimate(report: Path) -> float | None:
    """The JUnit total time, or None when the report cannot give one; the entry itself stays valid."""
    try:
        return _duration(_junit_total(report))
    except (OSError, ValueError, TypeError, KeyError, ET.ParseError):
        return None


def _duration(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        return None
    return value


def read_entries(cache_root: Path, repo_root: Path, repo: str | None, now: datetime) -> list[CacheEntry]:
    """This repository's cache entries recorded up to ``now``, newest first; unreadable files are skipped."""
    entries = []
    for path in cache_root.glob("*.json"):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not belongs(saved, path, repo_root, repo):
                continue
            created_at = datetime.fromisoformat(saved["created_at"])
            if created_at.tzinfo is None or created_at > now:
                continue
            estimated = False
            if "duration_seconds" in saved:
                duration = _duration(saved["duration_seconds"])
            elif path.with_suffix(".xml").is_file():
                # An older producer (tagged or not): the runner's estimate from the JUnit total time.
                duration = _estimate(path.with_suffix(".xml"))
                estimated = duration is not None
            else:
                duration = None
            entries.append(CacheEntry(path.stem, saved, created_at, duration, estimated,
                                      saved.get("timed_out") is True))
        except (OSError, ValueError, TypeError, KeyError, ET.ParseError):
            continue
    return sorted(entries, key=lambda entry: entry.created_at, reverse=True)


def latest_success(entries: list[CacheEntry]) -> CacheEntry | None:
    """The newest finished (not timed-out) capture with a known duration."""
    return next((entry for entry in entries if not entry.timed_out and entry.duration_seconds is not None), None)


def capture_limit(latest: float | None) -> int:
    """Seconds a capture may run, from the last successful capture's duration."""
    return DEFAULT_LIMIT if latest is None else int(min(MAX_LIMIT, max(DEFAULT_LIMIT, 2 * latest)))


def repository_limit(cache_root: Path, repo_root: Path, repo: str | None, now: datetime) -> tuple[int, float | None]:
    """The limit in force for this repository and the duration it came from."""
    latest = latest_success(read_entries(cache_root, repo_root, repo, now))
    duration = None if latest is None else latest.duration_seconds
    return capture_limit(duration), duration


def near_limit(durations: list[float], limit: int) -> bool:
    """``durations`` are measured successes, newest first."""
    if not durations:
        return False
    rising = len(durations) >= 3 and durations[2] < durations[1] < durations[0]
    return durations[0] / limit >= 0.75 or rising


def load_reusable(cache, key: str) -> dict | None:
    """A cached baseline a run may reuse: a timed-out capture never is."""
    saved = cache.load_acceptance_baseline(key)
    return None if saved is None or saved.get("timed_out") is True else saved


def record_timeout(cache, path: Path, saved: dict) -> bool:
    """Keep a timed-out capture's history unless a finished capture already holds its key."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and existing.get("timed_out") is not True:
            return False
    except (OSError, ValueError):
        pass
    cache._atomic_write(path, json.dumps(saved) + "\n")
    return True
