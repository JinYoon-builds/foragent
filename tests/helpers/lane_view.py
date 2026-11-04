"""Shared offline helpers for unit tests."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import re


def plain_frame(value):
    """Remove only terminal transport escapes, retaining all displayed characters."""
    value = re.sub(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)", "", value)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


def running_snapshot():
    """Extend the existing view builder with one lane for each factual state."""
    from foragent.labels import lane_display

    snapshot, config = done_snapshot(count=5)
    names = ("work", "queue", "human", "blocked", "finished")
    old_names = list(config.lanes)
    config = replace(config, repo="owner/repo", lanes={
        name: {"title": name, "emoji": "", "paths": [f"{name}/**"]} for name in names})
    rows = {}
    done = {}
    for index, (old, name) in enumerate(zip(old_names, names)):
        row = deepcopy(snapshot.status["lanes"][old])
        row.update(slug=name, lane_display=lane_display(config, name, "ko"))
        if name != "finished":
            number = 101 + index
            item = {"number": number, "title": f"Task {number}"}
            row.update(state=("working", "queued", "waiting-human", "blocked")[index],
                       issue=number, title=item["title"], last_activity_seconds=60,
                       elapsed_seconds=60, phase="implementing" if name == "work" else None,
                       queued=[item] if name == "queue" else [],
                       running=[item] if name == "work" else [])
        rows[name] = row
        done[name] = snapshot.done[old]
    status = {**snapshot.status, "lanes": rows,
              "summary": {"working": 1, "blocked": 1, "waiting_human": 1, "queued": 1},
              "active_runs": [{"number": 101, "lane": "work", "attempts": 1,
                               "last_activity_at": (snapshot.captured_at - timedelta(seconds=60)).isoformat()}]}
    return replace(snapshot, status=status, done=done, total=snapshot.completed + 4), config


def done_snapshot(*, language="ko", count=2, title="Current order"):
    from conftest import BASE_TIME
    from foragent.config import Config
    from foragent.labels import lane_display
    from foragent.tracker import FakeTracker
    from foragent.watch import Snapshot

    lanes = {f"lane-{i}": {"title": f"담당{i}" if language == "ko" else f"Agent{i}",
                           "emoji": "🧩", "paths": [f"area{i}/**"]} for i in range(count)}
    config = Config(language=language, lanes=lanes, skill_libraries=[])
    tracker = FakeTracker()
    tracker.add_issue(99, title)
    rows, done = {}, {}
    for index, slug in enumerate(lanes):
        rows[slug] = {"slug": slug, "lane_display": lane_display(config, slug, language),
                      "state": "idle", "issue": None, "title": None, "phase": None,
                      "elapsed_seconds": None, "last_activity_seconds": None,
                      "waiting_for": [], "running": [], "ready": [], "queued": []}
        done[slug] = []
        for offset in range(2 if index == 0 else 1):
            number = index * 10 + offset + 1
            tracker.add_issue(number, f"Completed work {number}", labels=[f"lane:{slug}"], state="closed")
            tracker.set_parent(number, 99)
            done[slug].append(tracker.get_issue(number))
    total = sum(map(len, done.values()))
    status = {"lanes": rows, "summary": {"working": 0, "blocked": 0, "waiting_human": 0, "queued": 0},
              "active_runs": [], "history": []}
    return Snapshot(status, done, tracker.get_issue(99), total, total, None, [], BASE_TIME), config


def bubble(frame):
    lines = plain_frame(frame).splitlines()
    first = next((i for i, line in enumerate(lines) if '╭' in line and '╮' in line), None)
    assert first is not None, 'the redesigned frame needs the orchestrator bubble'
    assert first + 7 < len(lines) and '╰' in lines[first + 7]
    body = []
    for line in lines[first+1:first+7]:
        left = min(i for i, ch in enumerate(line) if ch in '│┤')
        body.append(line[left+1:line.rindex('│')].strip())
    return '\n'.join(body).strip()
