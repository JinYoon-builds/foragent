"""Acceptance test for state-cache: each state log is parsed once per version.

The gate, the dashboard and `fa orch facts` read the same three logs many times:
runs.jsonl, orch-events.jsonl and issues.json. On 2026-11-04 one dashboard frame
took 2.7 s and 749 MB, and `fa orch facts` took 10.5 s. A log that did not change
is not parsed again; a log that changed is read again at once, so a reader sees
a new row right after it is appended.

Observation seams: every open of a state log goes through ``open``/``io.open``/
``os.open``; the test counts opens per path. Cache names and shapes are the lane's.
"""

import builtins
import io
import os
from collections import Counter
from contextlib import contextmanager

import pytest

from foragent import orch_facts, status, watch_data
from foragent.orch_events import append_event, read_events
from test_orch_review_handoff import make_case

LOGS = {"runs.jsonl", "orch-events.jsonl", "issues.json"}


@contextmanager
def counting_opens(monkeypatch):
    """Every open of a state log, by path, while the block runs."""
    opened = Counter()
    real_open, real_os_open = builtins.open, os.open

    def note(file, writes):
        # Only reads count: writing a log (or a frozen copy of it) is not parsing it.
        if not writes and isinstance(file, (str, os.PathLike)) and os.path.basename(os.fspath(file)) in LOGS:
            opened[os.path.realpath(os.fspath(file))] += 1

    def counted_open(file, mode="r", *args, **kwargs):
        note(file, any(flag in mode for flag in "wax+"))
        return real_open(file, mode, *args, **kwargs)

    def counted_os_open(file, flags, *args, **kwargs):
        note(file, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND)))
        return real_os_open(file, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", counted_open)
        patch.setattr(io, "open", counted_open)
        patch.setattr(os, "open", counted_os_open)
        yield opened


@pytest.fixture
def logs(git_history, tmp_path, monkeypatch):
    """A store with all three logs: finished runs, orchestrator events and an issue snapshot."""
    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    case.store.finish_run(1, outcome="retries-exceeded")
    for number in range(2, 12):
        case.tracker.add_issue(number, f"Task {number}", case.tracker.get_issue(1).body, labels=["approved"])
        case.store.start_run(number, branch=f"foragent/issue-{number}", worktree=case.store.worktree_path(number))
        case.store.finish_run(number, outcome="merged")
    for index in range(40):
        append_event(case.store, case.clock[0], 1 + index % 5, "note", f"event {index}")
    case.store.refresh_local_issues(case.tracker.list_issues(), [], {}, case.clock[0])
    for name in LOGS:
        assert (case.store.root / name).is_file(), name
    case.originals = {os.path.realpath(case.store.root / name) for name in LOGS}
    return case


def frame(case):
    """One dashboard frame: the shared status payload plus the dashboard's own facts."""
    payload = status.collect(case.store, case.config, case.clock[0])
    return watch_data.collect(payload, case.tracker, case.store, case.config, case.clock[0],
                              repo_root=case.root)


def facts(case):
    """The data `fa orch facts --json` prints."""
    return orch_facts.collect(case.store, case.config, case.root)


def test_state_cache_unchanged_logs_are_not_parsed_again(logs, monkeypatch):
    case = logs
    frame(case)
    with counting_opens(monkeypatch) as opened:
        frame(case)
        frame(case)
    assert not opened, f"Frames over unchanged logs must not open them again: {dict(opened)}"

    with counting_opens(monkeypatch) as opened:
        facts(case)
    repeated = {path: count for path, count in opened.items() if count > 1}
    assert not repeated, (
        f"One `fa orch facts` must parse each log version at most once; opened again: {repeated}"
    )
    with counting_opens(monkeypatch) as opened:
        facts(case)
    again = {path: count for path, count in opened.items() if path in case.originals and count > 1}
    assert not again, f"A second facts call over unchanged logs must not read them again and again: {again}"


def test_state_cache_a_changed_log_is_read_at_once(logs, monkeypatch):
    case = logs
    frame(case)
    events = len(read_events(case.store))

    append_event(case.store, case.clock[0], 1, "note", "the newest event")
    case.store.start_run(1, branch="foragent/issue-1", worktree=case.store.worktree_path(1))
    case.store.finish_run(1, outcome="merged")
    case.tracker.add_issue(40, "A new issue", "body", labels=["approved"])
    case.store.refresh_local_issues(case.tracker.list_issues(), [], {}, case.clock[0])

    seen = read_events(case.store)
    assert len(seen) == events + 1 and seen[-1]["summary"] == "the newest event", (
        "A reader must see an appended event right after the append"
    )
    newest = [run for run in case.store.read_history() if run.number == 1][-1]
    assert newest.outcome == "merged", f"A reader must see the new run row of #1 at once: {newest.outcome}"
    assert 40 in {row["number"] for row in case.store.local_issues()}, "A reader must see the new issue snapshot"
    shown = frame(case)
    assert any(event.get("summary") == "the newest event" for event in watch_data.recent_events(case.store)), (
        "The dashboard must show the new event on its next frame"
    )
    assert shown is not None
    # The repeat after the change is again free: the new version is parsed once, then reused.
    with counting_opens(monkeypatch) as opened:
        frame(case)
    assert not opened, f"After one re-read, the new version must be reused: {dict(opened)}"
