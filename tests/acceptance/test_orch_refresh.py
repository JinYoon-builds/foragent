"""Acceptance test for orch-refresh. Locked by Foragent; do not edit."""

import importlib
import json
from datetime import datetime, timedelta
from types import ModuleType
import sys

import pytest
import yaml

from test_heartbeats import heartbeats_write
from test_orch_review_handoff import bind_cli, make_case
from test_skills_autosync import cli_exit, isolated_home  # noqa: F401


# Observable Claude Code marker: a top-level type=system, subtype=compact_boundary
# record. A following isCompactSummary user message is not another compaction.
# https://code.claude.com/docs/en/agent-sdk/agent-loop#automatic-compaction
#
# Wire assumptions for this locked contract:
# - Extend #127's orch-heartbeat.json with session_id, transcript_path, started_at
#   and status. refresh-due JSON uses those first two keys, compactions,
#   age_seconds, thresholds (using the config key names below), due and reason.
# - Either threshold is inclusive. N is five minutes, also inclusive; a person
#   request has the explicit inbox source "person", and pending means not done.
# - Handoff freshness uses its accepted timestamp, strictly after the last
#   refresh; neither transcript mtime nor the latest heartbeat is session start.
#   The schedule is advisory: --ready with a fresh note can refresh a new
#   session too, without waiting for another schedule threshold.
# - #128's shared launch seam is foragent.orch_spawn.spawn_orchestrator(
#   repo_root, store, config, *, prompt, now), returning a session descriptor
#   {session_id, transcript_path, started_at}. ensure_orchestrator may use this
#   lower-level seam too. The fake launches only: it never updates heartbeat,
#   handoff or event state on behalf of the refresh implementation.
# - Retired heartbeat records have session_id and status="retired"; their
#   archive filename/nesting and the stored note's filename are unrestricted.

DEFAULT_THRESHOLDS = {"orch_refresh_compactions": 4, "orch_refresh_hours": 24}
PERSON_QUIET_MINUTES = 5
OLD_SESSION = "11111111-2222-4333-8444-555555555555"
SECTIONS = {
    "Current work": "Finish the #376 swap-in order; the orch lane is paused for its schema change.",
    "Watches": (
        "The #376 merge watch guards removal of temporary dependency links. "
        "Re-arm it with `fa orch watch --follow` if its watcher exits."
    ),
    "Open promises": "Remove temporary dependency links when #376 merges, even if its lane is idle.",
    "Temporary settings": (
        "parallel_floor: 5 keeps the swap-in experiment moving; revert to 1 "
        "when the agreed swap-in threshold is reached."
    ),
    "Queued orders not yet published": (
        "The refresh order is drafted, not published; its locked test draft is "
        "scratch/orders/refresh/tests/acceptance/test_orch_refresh.py."
    ),
    "Pending decisions for the person": "none",
    "Peers": "Planning session address: planning-session@foragent-f1.",
    "Recent incidents": "The old watch exited during compaction; its replacement is already armed.",
}


def note_text(sections=None):
    sections = SECTIONS if sections is None else sections
    return "# Orchestrator handoff\n\n" + "\n".join(
        f"## {heading}\n\n{body}\n" for heading, body in sections.items()
    )


def boundary(session_id, at, index):
    return {"type": "system", "subtype": "compact_boundary", "sessionId": session_id,
            "uuid": f"boundary-{index}", "timestamp": at.isoformat(),
            "compactMetadata": {"trigger": "auto", "preTokens": 180000}}


def transcript(path, session_id, started_at, compactions):
    rows = [{"type": "user", "sessionId": session_id, "timestamp": started_at.isoformat(),
             "message": {"role": "user", "content": "Start orchestrating."}}]
    for index in range(compactions):
        at = started_at + timedelta(seconds=index + 1)
        rows.extend([
            boundary(session_id, at, index),
            {"type": "user", "sessionId": session_id, "timestamp": at.isoformat(),
             "isCompactSummary": True,
             "message": {"role": "user", "content": "Summary of the preceding work."}},
        ])
    # Text mentioning the marker and an unrelated system record do not count.
    rows.extend([
        {"type": "assistant", "message": {"role": "assistant", "content":
            [{"type": "text", "text": '{"type":"system","subtype":"compact_boundary"}'}]}},
        {"type": "system", "subtype": "turn_duration", "durationMs": 4000},
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def refresh_case(git_history, tmp_path, monkeypatch, isolated_home):
    """Real CLI/state, fake clock/transcripts/launcher, and no external effects."""
    import os
    import signal
    import socket
    import subprocess
    import tempfile

    import foragent
    from foragent import notify

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], notify="none")
    case.main = bind_cli(case, monkeypatch)
    case.spawned = []
    case.notifications = []
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for name in ("FORAGENT_ORCH_SPAWN", "ORCA_TERMINAL_HANDLE", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(name, raising=False)

    try:
        spawn_module = importlib.import_module("foragent.orch_spawn")
    except ModuleNotFoundError as exc:
        if exc.name != "foragent.orch_spawn":
            raise
        spawn_module = ModuleType("foragent.orch_spawn")
        monkeypatch.setitem(sys.modules, spawn_module.__name__, spawn_module)
        monkeypatch.setattr(foragent, "orch_spawn", spawn_module, raising=False)

    def spawn(repo_root, store, config, *, prompt, now):
        assert repo_root == case.root and store.root == case.store.root
        assert config.repo == "owner/repo" and now == case.clock[0]
        assert isinstance(prompt, str) and prompt.strip(), "a fresh session needs its first prompt"
        session_id = f"aaaaaaaa-bbbb-4ccc-8ddd-{len(case.spawned) + 1:012d}"
        path = tmp_path / "transcripts" / f"{session_id}.jsonl"
        transcript(path, session_id, now, 0)
        result = {"session_id": session_id, "transcript_path": str(path),
                  "started_at": now.isoformat()}
        case.spawned.append({"prompt": prompt, "session": result})
        return dict(result)

    original = getattr(spawn_module, "spawn_orchestrator", None)
    # Cover aliases imported before fixture setup, as well as lazy imports.
    if original is not None:
        for name, module in tuple(sys.modules.items()):
            if name.startswith("foragent.") and module is not None:
                for attr, value in tuple(vars(module).items()):
                    if value is original:
                        monkeypatch.setattr(module, attr, spawn)
    monkeypatch.setattr(spawn_module, "spawn_orchestrator", spawn, raising=False)

    def notify_offline(*args, **kwargs):
        case.notifications.append((args, kwargs))
        return True

    monkeypatch.setattr(notify, "send", notify_offline)

    def forbidden(*args, **kwargs):
        pytest.fail("orch-refresh must stay offline: no real process, terminal, signal or network")

    for module, names in (
        (subprocess, ("run", "Popen")), (os, ("system", "kill", "killpg")),
        (signal, ("raise_signal", "pthread_kill", "pidfd_send_signal")),
        (socket, ("create_connection",)), (socket.socket, ("connect", "connect_ex")),
    ):
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)
    return case


def seed_session(case, *, compactions=4, age_seconds=3600):
    started_at = case.clock[0] - timedelta(seconds=age_seconds)
    path = case.root.parent / "transcripts" / f"{OLD_SESSION}.jsonl"
    transcript(path, OLD_SESSION, started_at, compactions)
    heartbeat = heartbeats_write(case.store, "orch", case.clock[0])
    record = json.loads(heartbeat.read_text(encoding="utf-8"))
    record.update(session_id=OLD_SESSION, transcript_path=str(path),
                  started_at=started_at.isoformat(), status="active")
    heartbeat.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return heartbeat, path


def fa(case, capsys, *args):
    capsys.readouterr()
    code = cli_exit(case.main, ["orch", *map(str, args), "--repo-root", str(case.root)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def fa_ok(case, capsys, *args):
    code, out, err = fa(case, capsys, *args)
    assert code == 0, f"fa orch {' '.join(map(str, args))} must succeed (exit={code}):\n{out}{err}"
    return out


def refused(code, out, err):
    output = out + err
    assert code != 0, "the command must refuse this request"
    assert "invalid choice:" not in output and "unrecognized arguments:" not in output, (
        f"the CLI must handle the requested orch command before refusing it:\n{output}")
    return output


def due_report(case, capsys):
    report = json.loads(fa_ok(case, capsys, "refresh-due", "--json"))
    assert isinstance(report, dict), "refresh-due must report a JSON object"
    assert isinstance(report.get("reason"), str) and report["reason"].strip(), report
    assert "ready" not in report and "readiness" not in report, "readiness belongs to the orchestrator"
    return report


def refresh_events(case):
    path = case.store.root / "orch-events.jsonl"
    if not path.exists():
        return []
    return [row for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            if (row := json.loads(line)).get("kind") == "refresh"]


def accept_note(case, capsys, text=None):
    text = note_text() if text is None else text
    source = case.root / "NOTE.md"
    source.write_text(text, encoding="utf-8")
    fa_ok(case, capsys, "handoff", "--file", source)
    copies = [path for path in case.store.root.rglob("*")
              if path.is_file() and path.read_bytes() == text.encode("utf-8")]
    assert copies, "handoff must store a verbatim note under the state folder"
    assert copies[0].resolve().is_relative_to(case.store.root.resolve()), "the note must be a stored copy"
    return copies[0]


def person_request(case, capsys, *, source="person", status="new", age_seconds=0):
    now = case.clock[0]
    case.clock[0] = now - timedelta(seconds=age_seconds)
    try:
        fa_ok(case, capsys, "request", "--from", source, "--text", "I am discussing the next order.")
        rows = json.loads(fa_ok(case, capsys, "inbox", "--json"))
        request_id = rows[-1]["id"]
        if status != "new":
            fa_ok(case, capsys, "request-status", request_id, status, "--note", "Recorded by the test.")
    finally:
        case.clock[0] = now
    return request_id


def state_records(value):
    """Allow either archived heartbeat files or a history in the current file."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from state_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from state_records(child)


def assert_refreshed(case, heartbeat, old_session, *, count=1):
    assert len(case.spawned) == count, "refresh must launch exactly one fresh session per accepted call"
    current = json.loads(heartbeat.read_text(encoding="utf-8"))
    session = case.spawned[-1]["session"]
    assert current["session_id"] == session["session_id"] != old_session
    assert current["transcript_path"] == session["transcript_path"]
    records = [row for path in case.store.root.rglob("*.json")
               for row in state_records(json.loads(path.read_text(encoding="utf-8")))]
    assert any(row.get("session_id") == old_session and row.get("status") == "retired"
               for row in records), "the outgoing session must be durably retired in heartbeat state"
    events = refresh_events(case)
    assert len(events) == count, "an accepted refresh must append exactly one refresh event"
    assert datetime.fromisoformat(events[-1]["at"]) == case.clock[0]


@pytest.mark.parametrize("compactions,age_seconds,expected", [
    (0, 0, False), (3, 24 * 3600 - 1, False),
    (4, 3600, True), (0, 24 * 3600, True), (7, 30 * 3600, True),
], ids=["new", "below-both", "compaction-boundary", "age-boundary", "both-due"])
def test_orch_refresh_due_reports_heartbeat_facts_and_inclusive_defaults(
    refresh_case, capsys, compactions, age_seconds, expected,
):
    case = refresh_case
    heartbeat, path = seed_session(case, compactions=compactions, age_seconds=age_seconds)
    before = heartbeat.read_bytes()
    report = due_report(case, capsys)
    assert report["session_id"] == OLD_SESSION and report["transcript_path"] == str(path)
    assert report["compactions"] == compactions, "count boundary records, not summary text or turns"
    assert report["age_seconds"] == age_seconds, "age starts at session start, not the last heartbeat"
    assert report["thresholds"].items() >= DEFAULT_THRESHOLDS.items()
    assert report["due"] is expected
    reason = report["reason"].casefold()
    if expected:
        assert ((compactions >= 4 and "compaction" in reason)
                or (age_seconds >= 24 * 3600 and ("age" in reason or "hour" in reason))), report
    assert heartbeat.read_bytes() == before and case.spawned == [] and refresh_events(case) == []


@pytest.mark.parametrize("count_limit,hours_limit,compactions,age_seconds,expected", [
    (2, 6, 2, 3600, True), (2, 6, 1, 6 * 3600, True),
    (2, 6, 1, 6 * 3600 - 1, False), (8, 48, 4, 24 * 3600, False),
], ids=["lower-count", "lower-age", "below-custom", "raise-both"])
def test_orch_refresh_due_loads_configured_thresholds(
    refresh_case, capsys, monkeypatch, count_limit, hours_limit, compactions, age_seconds, expected,
):
    from foragent import cli
    from foragent.config import load_config

    case = refresh_case
    values = {"orch_refresh_compactions": count_limit, "orch_refresh_hours": hours_limit}
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({"repo": "owner/repo", **values}), encoding="utf-8")
    # Exercise the real config loader, not extra attributes attached to a fake Config.
    monkeypatch.setattr(cli, "load_config", load_config)
    seed_session(case, compactions=compactions, age_seconds=age_seconds)
    report = due_report(case, capsys)
    assert report["thresholds"].items() >= values.items()
    assert report["compactions"] == compactions and report["age_seconds"] == age_seconds
    assert report["due"] is expected and case.spawned == []


def test_orch_refresh_due_rereads_boundaries_without_deciding_readiness(refresh_case, capsys):
    case = refresh_case
    heartbeat, path = seed_session(case, compactions=3)
    before = heartbeat.read_bytes()
    person_request(case, capsys)  # A person is talking; no handoff has been written.
    initial = due_report(case, capsys)
    assert initial["compactions"] == 3 and initial["due"] is False
    case.clock[0] += timedelta(seconds=1)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(boundary(OLD_SESSION, case.clock[0], 3)) + "\n")
    report = due_report(case, capsys)
    assert report["compactions"] == 4 and report["due"] is True
    assert report["reason"] != initial["reason"], "explain which measured threshold became due"
    assert heartbeat.read_bytes() == before and case.spawned == [] and refresh_events(case) == []


def test_orch_refresh_handoff_stores_all_operational_context_verbatim(refresh_case, capsys):
    case = refresh_case
    stored = accept_note(case, capsys)
    # A pointer to the outgoing session's draft would lose the note when edited.
    (case.root / "NOTE.md").write_text("The outgoing draft has changed.\n", encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == note_text()
    assert case.spawned == [] and refresh_events(case) == []


def test_orch_refresh_handoff_accepts_explicit_none_in_every_section(refresh_case, capsys):
    text = note_text({heading: "none" for heading in SECTIONS})
    stored = accept_note(refresh_case, capsys, text)
    assert stored.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("section", SECTIONS, ids=lambda value: value.lower().replace(" ", "-"))
@pytest.mark.parametrize("defect", ["missing", "empty"])
def test_orch_refresh_handoff_refuses_each_missing_or_empty_section(
    refresh_case, capsys, section, defect,
):
    case = refresh_case
    sections = dict(SECTIONS)
    if defect == "missing":
        del sections[section]
    else:
        sections[section] = " \t\n  "
    source = case.root / "incomplete.md"
    text = note_text(sections)
    source.write_text(text, encoding="utf-8")
    code, out, err = fa(case, capsys, "handoff", "--file", source)
    output = refused(code, out, err)
    assert section.casefold() in output.casefold(), f"handoff must name the {defect} section: {section}\n{output}"
    assert not any(path.is_file() and path.read_bytes() == text.encode("utf-8")
                   for path in case.store.root.rglob("*")), "a refused note must not be stored as a handoff"
    assert case.spawned == [] and refresh_events(case) == []


def test_orch_refresh_ready_refuses_without_a_valid_handoff(refresh_case, capsys):
    case = refresh_case
    heartbeat, _ = seed_session(case)
    before = heartbeat.read_bytes()
    code, out, err = fa(case, capsys, "refresh", "--ready")
    output = refused(code, out, err)
    assert "handoff" in output.casefold(), f"refresh --ready must explain the required handoff:\n{output}"
    assert heartbeat.read_bytes() == before and case.spawned == [] and refresh_events(case) == []


def test_orch_refresh_requires_the_outgoing_orchestrator_to_confirm_ready(refresh_case, capsys):
    case = refresh_case
    heartbeat, _ = seed_session(case)
    accept_note(case, capsys)
    before = heartbeat.read_bytes()
    code, out, err = fa(case, capsys, "refresh")
    output = refused(code, out, err)
    assert "--ready" in output, "a due session and a note do not confirm a quiet moment"
    assert heartbeat.read_bytes() == before and case.spawned == [] and refresh_events(case) == []


@pytest.mark.parametrize("source,status,age_seconds,blocked", [
    ("person", "new", 0, True), ("person", "processing", 60, True),
    ("person", "new", PERSON_QUIET_MINUTES * 60, True),
    ("person", "new", PERSON_QUIET_MINUTES * 60 + 1, False),
    ("person", "done", 0, False), ("planning", "new", 0, False), ("gate", "new", 0, False),
], ids=["new-person", "processing-person", "quiet-boundary", "old-person", "done-person", "planning", "gate"])
def test_orch_refresh_ready_observes_pending_person_messages_in_the_quiet_window(
    refresh_case, capsys, source, status, age_seconds, blocked,
):
    case = refresh_case
    heartbeat, _ = seed_session(case)
    request_id = person_request(case, capsys, source=source, status=status, age_seconds=age_seconds)
    accept_note(case, capsys)
    before = heartbeat.read_bytes()
    code, out, err = fa(case, capsys, "refresh", "--ready")
    if blocked:
        output = refused(code, out, err)
        assert "inbox" in output.casefold(), f"explain the pending inbox message:\n{output}"
        assert heartbeat.read_bytes() == before and case.spawned == [] and refresh_events(case) == []
        # Resolving the message makes the same validated note usable; refusal
        # must not consume it, mark the message done or retire the session.
        rows = json.loads(fa_ok(case, capsys, "inbox", "--json"))
        assert next(row for row in rows if row["id"] == request_id)["status"] == status
        fa_ok(case, capsys, "request-status", request_id, "done", "--note", "Conversation finished.")
        fa_ok(case, capsys, "refresh", "--ready")
    else:
        assert code == 0, f"only pending recent person messages defer refresh:\n{out}{err}"
    assert_refreshed(case, heartbeat, OLD_SESSION)


def test_orch_refresh_ready_launches_with_handoff_and_required_reading_then_retires_old_session(
    refresh_case, capsys,
):
    case = refresh_case
    heartbeat, old_transcript = seed_session(case)
    old_bytes = old_transcript.read_bytes()
    stored = accept_note(case, capsys)
    case.clock[0] += timedelta(seconds=1)
    fa_ok(case, capsys, "refresh", "--ready")
    assert_refreshed(case, heartbeat, OLD_SESSION)
    prompt = case.spawned[0]["prompt"]
    assert str(stored) in prompt, "the first prompt must name the durable handoff note"
    for reading in ("docs/decisions.md", "fa orch queue", "fa orch plan",
                    "fa orch inbox --json", "fa orch questions --json", "fa orch log"):
        assert reading in prompt, f"the fresh orchestrator needs to read {reading} before acting"
    normalized = " ".join(prompt.casefold().split())
    assert "read" in normalized and "before" in normalized and "act" in normalized, (
        "the first prompt must instruct the fresh session to read its context before acting")
    assert old_transcript.read_bytes() == old_bytes and stored.read_text(encoding="utf-8") == note_text()


@pytest.mark.parametrize("replacement", ["none", "same-timestamp"])
def test_orch_refresh_ready_requires_a_handoff_strictly_newer_than_the_last_refresh(
    refresh_case, capsys, replacement,
):
    case = refresh_case
    heartbeat, _ = seed_session(case)
    accept_note(case, capsys)
    case.clock[0] += timedelta(seconds=1)
    fa_ok(case, capsys, "refresh", "--ready")
    assert_refreshed(case, heartbeat, OLD_SESSION)
    current_session = case.spawned[-1]["session"]["session_id"]
    if replacement == "same-timestamp":
        accept_note(case, capsys)  # Equal to last refresh is still not newer.
    case.clock[0] += timedelta(seconds=1)
    before = heartbeat.read_bytes()
    code, out, err = fa(case, capsys, "refresh", "--ready")
    output = refused(code, out, err)
    assert "handoff" in output.casefold(), "a consumed or same-time handoff cannot be reused"
    assert heartbeat.read_bytes() == before and len(case.spawned) == len(refresh_events(case)) == 1
    accept_note(case, capsys)
    case.clock[0] += timedelta(seconds=1)
    fa_ok(case, capsys, "refresh", "--ready")
    assert_refreshed(case, heartbeat, current_session, count=2)


def test_orch_refresh_ready_refuses_a_stored_note_that_is_no_longer_valid(refresh_case, capsys):
    case = refresh_case
    heartbeat, _ = seed_session(case)
    stored = accept_note(case, capsys)
    stored.write_text(note_text({**SECTIONS, "Watches": " \n"}), encoding="utf-8")
    before = heartbeat.read_bytes()
    code, out, err = fa(case, capsys, "refresh", "--ready")
    output = refused(code, out, err)
    assert "handoff" in output.casefold(), "--ready still requires a valid stored handoff"
    assert heartbeat.read_bytes() == before and case.spawned == [] and refresh_events(case) == []


def test_orch_refresh_due_counts_only_the_new_session_after_refresh(refresh_case, capsys):
    case = refresh_case
    heartbeat, old_transcript = seed_session(case, compactions=7, age_seconds=30 * 3600)
    accept_note(case, capsys)
    case.clock[0] += timedelta(seconds=1)
    fa_ok(case, capsys, "refresh", "--ready")
    assert_refreshed(case, heartbeat, OLD_SESSION)
    session = case.spawned[0]["session"]
    case.clock[0] += timedelta(seconds=2)
    report = due_report(case, capsys)
    assert report["session_id"] == session["session_id"] != OLD_SESSION
    assert report["transcript_path"] == session["transcript_path"] != str(old_transcript)
    assert report["compactions"] == 0 and report["age_seconds"] == 2 and report["due"] is False
    assert len(case.spawned) == len(refresh_events(case)) == 1, "reading fresh facts must not launch again"
