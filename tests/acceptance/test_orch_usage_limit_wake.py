"""Acceptance tests for orch-usage-limit-wake.

Bug 2026-11-06: the orchestrator (a Claude session) answered every event with
"You've hit your 5-hour limit · resets 12:10pm (Asia/Seoul)" from 11:31 KST, nobody
was alarmed, and nothing woke it after the reset at 12:10.

Chosen observable contract (the detection, record fields and wake mechanism stay the
lane's choice):

- Input: the orchestrator's own Claude transcript, the file the active heartbeat
  session names as ``transcript_path``. A usage-limit reply is written there the way
  Claude Code writes it (a synthetic assistant message with ``isApiErrorMessage``).
  Its ``fa orch watch --follow`` Monitor keeps the heartbeat fresh meanwhile.
- The gate is the real ``fa gate tick`` entry point.
- "The PM gets one alarm": the PM-facing channels are the inbox requests and
  questions, the inbox notices and pushes. Every channel holds at most one item about
  the limit and at least one channel holds exactly one.
- "The /fa report names it": ``fa brief --report`` (the report /fa shows) names the
  usage limit and, when the message gives one, the reset time (12:10 KST or 03:10 UTC).
- "Wakes the orchestrator": a new event its ``fa orch watch --follow`` prints (a new
  inbox request or a newly answered question, ``orch_inbox.FollowEvents``) or a
  ``claude`` launch (a fake ``claude`` on PATH records every call). Met role-signal
  requests (the idle repository's lane-supply signal) are other events and not counted.
- "The alarm clears": no alarm item stays open, a report that covers only the time
  after the recovery no longer names the limit, and a later limit reply alarms again.
"""

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import uuid

import pytest
import yaml

from test_skills_autosync import cli_exit, isolated_home  # noqa: F401 (fixture)
from test_tick_failure_alert import tick_case  # noqa: F401 (fixture)


LIMIT_AT = datetime(2026, 11, 6, 2, 31, 5, tzinfo=UTC)  # 11:31 KST
RESET_AT = datetime(2026, 11, 6, 3, 10, tzinfo=UTC)  # 12:10 KST
LIMIT_TEXT = "You've hit your 5-hour limit · resets 12:10pm (Asia/Seoul)"
LIMIT_NO_RESET = "You've hit your usage limit"
RESET_TIME = re.compile(r"(?<![\d:])(?:12:10|0?3:10)(?![\d])")
LIMIT_WORDS = re.compile(r"limit|한도|리밋", re.I)
USAGE_WORDS = re.compile(r"usage|5-hour|5 hour|hit your|사용량|사용 한도|5시간", re.I)


def is_limit_alarm(text) -> bool:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return bool(LIMIT_WORDS.search(text) and USAGE_WORDS.search(text))


class Orchestrator:
    """The orchestrator's active session, its transcript and its live watch heartbeat."""

    def __init__(self, case):
        from foragent import orch_sessions
        from foragent.orch_spawn_launch import transcript_path

        self.case = case
        self.session_id = str(uuid.uuid4())
        self.path = Path(transcript_path(case.root.resolve(), self.session_id))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        started = LIMIT_AT - timedelta(hours=2)
        orch_sessions.activate(case.store, {"session_id": self.session_id, "transcript_path": str(self.path),
                                            "started_at": started.isoformat()}, started)
        self.parent = None
        self.event("Watching the gate.", "Watching the gate; nothing is blocked.", started)

    def beat(self, at):
        """``fa orch watch --follow`` renews the heartbeat whatever the session replies."""
        self.case.store.write_heartbeat("orchestrator", at, session_id=self.session_id)

    def _line(self, row):
        row.update({"parentUuid": self.parent, "isSidechain": False, "userType": "external",
                    "cwd": str(self.case.root), "sessionId": self.session_id, "version": "2.1.30",
                    "gitBranch": "main", "uuid": str(uuid.uuid4())})
        self.parent = row["uuid"]
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def event(self, prompt, reply, at, *, limited=False):
        """One delivered event (a Monitor line) and the session's reply to it."""
        stamp = lambda moment: moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
        self._line({"type": "user", "timestamp": stamp(at),
                    "message": {"role": "user", "content": f"<task-notification>{prompt}</task-notification>"}})
        message = {"id": f"msg_{uuid.uuid4().hex[:20]}", "type": "message", "role": "assistant",
                   "model": "<synthetic>" if limited else "claude-opus-4-5",
                   "content": [{"type": "text", "text": reply}],
                   "stop_reason": "stop_sequence" if limited else "end_turn", "stop_sequence": "",
                   "usage": {"input_tokens": 0 if limited else 1200, "output_tokens": 0 if limited else 40}}
        row = {"type": "assistant", "timestamp": stamp(at + timedelta(seconds=1)), "message": message}
        if limited:
            row.update({"isApiErrorMessage": True, "error": "rate_limit"})
        self._line(row)


@pytest.fixture
def limit_case(tick_case, tmp_path, monkeypatch):  # noqa: F811
    from foragent import orch_inbox

    case = tick_case
    # The real gate decides spawns and silence too: a live, beating orchestrator opens nothing.
    monkeypatch.delenv("FORAGENT_ORCH_SPAWN", raising=False)
    # Transcripts live under the isolated home, never in the developer's Claude profile.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "skill_libraries": [], "notify": "claude-push", "language": "en",
    }), encoding="utf-8")
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    case.launch_log = tmp_path / "claude-calls.jsonl"
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\n"
                      f"python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \"$@\" >> '{case.launch_log}'\n"
                      "exit 0\n", encoding="utf-8")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    case.clock[0] = LIMIT_AT - timedelta(minutes=30)
    case.orch = Orchestrator(case)
    case.follow = None
    case.inbox = orch_inbox
    return case


def tick(case, capsys, at):
    """One real ``fa gate tick`` at ``at``, in a fresh process-like store."""
    from foragent.state import StateStore

    case.clock[0] = at
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.orch.beat(at)
    capsys.readouterr()
    code = cli_exit(case.main, ["gate", "tick", "--json", "--repo-root", str(case.root)])
    output = capsys.readouterr()
    assert code == 0, output.err


def report(case, capsys, at, *full):
    case.clock[0] = at
    capsys.readouterr()
    assert cli_exit(case.main, ["brief", "--report", *full, "--repo-root", str(case.root)]) == 0
    return capsys.readouterr().out


def channels(case):
    """Every PM-facing item about the usage limit, by channel."""
    inbox = case.inbox
    notices, _ = inbox.recent_notices(case.store, LIMIT_AT - timedelta(days=2))
    return {
        "requests": [row for row in inbox.requests(case.store) if is_limit_alarm(row)],
        "questions": [row for row in inbox.questions(case.store) if is_limit_alarm(row)]
        + [row for row in _closed_questions(case) if is_limit_alarm(row)],
        "notices": [row for row in notices if is_limit_alarm(row)],
        "pushes": [row for row in case.pushes if is_limit_alarm(row)],
    }


def _closed_questions(case):
    folder = case.store.root / "inbox" / "questions"
    rows = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("id") not in {item["id"] for item in case.inbox.questions(case.store)}:
            rows.append(row)
    return rows


def assert_one_alarm(case, *, with_reset: bool):
    found = channels(case)
    doubled = {name: len(items) for name, items in found.items() if len(items) > 1}
    assert not doubled, f"The PM must get one usage-limit alarm, not one per tick or event: {doubled}"
    sent = {name: items[0] for name, items in found.items() if items}
    assert sent, ("When the orchestrator replies with a usage-limit message the PM must get one alarm "
                  "(inbox request, question, notice or push); none was found")
    if with_reset:
        assert any(RESET_TIME.search(json.dumps(item, ensure_ascii=False)) for item in sent.values()), (
            f"The alarm must carry the reset time the message names (12:10 KST): {sent}"
        )
    return sent


def launches(case):
    if not case.launch_log.exists():
        return []
    return [json.loads(line) for line in case.launch_log.read_text(encoding="utf-8").splitlines() if line]


def start_wake_count(case):
    case.follow = case.inbox.FollowEvents(case.store)
    case.launches_before = len(launches(case))


def wakes(case):
    """Wake-ups since ``start_wake_count``: watch events and Claude launches."""
    signals = {row["id"] for row in case.inbox.requests(case.store)
               if row.get("from") == "gate" and row.get("role") and isinstance(row.get("signal"), dict)}
    # Met role signals of the idle test repository (lane supply) are other gate events, not the wake.
    events = [event for event in case.follow.poll(case.store) if event.get("id") not in signals]
    case.follow_events = getattr(case, "follow_events", []) + events
    return case.follow_events + launches(case)[case.launches_before:]


def limited_until_reset(case, capsys, text=LIMIT_TEXT):
    """Normal work, then the limit reply at 11:31 KST and the same reply to every later event."""
    tick(case, capsys, LIMIT_AT - timedelta(minutes=10))
    assert not any(channels(case).values()), "A working orchestrator must raise no usage-limit alarm"
    case.orch.event("Lane supply is low.", text, LIMIT_AT, limited=True)
    tick(case, capsys, LIMIT_AT + timedelta(minutes=1))


def test_limit_reply_gives_pm_one_alarm_with_reset_and_report_names_it(limit_case, capsys):
    case = limit_case
    limited_until_reset(case, capsys)
    first = assert_one_alarm(case, with_reset=True)
    # Every later event gets the same reply; the gate keeps ticking.
    for minutes in (8, 14, 29, 37):
        at = LIMIT_AT + timedelta(minutes=minutes)
        case.orch.event(f"PM request r{minutes}.", LIMIT_TEXT, at - timedelta(seconds=30), limited=True)
        tick(case, capsys, at)
    later = assert_one_alarm(case, with_reset=True)
    assert set(later) == set(first), "Later limited replies must not add an alarm on another channel"

    out = report(case, capsys, LIMIT_AT + timedelta(minutes=38))
    assert is_limit_alarm(out), f"The /fa report must name the orchestrator's usage limit:\n{out}"
    assert RESET_TIME.search(out), f"The /fa report must give the reset time (12:10 KST):\n{out}"


def test_limit_reply_without_reset_still_alarms_once(limit_case, capsys):
    case = limit_case
    limited_until_reset(case, capsys, LIMIT_NO_RESET)
    for minutes in (10, 20):
        at = LIMIT_AT + timedelta(minutes=minutes)
        case.orch.event("Lane supply is low.", LIMIT_NO_RESET, at - timedelta(seconds=30), limited=True)
        tick(case, capsys, at)
    assert_one_alarm(case, with_reset=False)
    out = report(case, capsys, LIMIT_AT + timedelta(minutes=21))
    assert is_limit_alarm(out), f"The /fa report must name the usage limit even without a reset time:\n{out}"


def test_gate_wakes_orchestrator_once_at_reset_and_alarm_clears(limit_case, capsys):
    case = limit_case
    limited_until_reset(case, capsys)
    start_wake_count(case)
    for minutes in (14, 29, 38):
        at = LIMIT_AT + timedelta(minutes=minutes)
        case.orch.event("Lane supply is low.", LIMIT_TEXT, at - timedelta(seconds=30), limited=True)
        tick(case, capsys, at)
    assert wakes(case) == [], "Before the reset time a wake only meets the same limit; the gate must wait"
    alarms = {name: [row.get("id") for row in items] for name, items in channels(case).items()}

    # The reset passes; no event reaches the orchestrator until the gate wakes it.
    for at in (RESET_AT + timedelta(seconds=30), RESET_AT + timedelta(minutes=1, seconds=30),
               RESET_AT + timedelta(minutes=3)):
        tick(case, capsys, at)
    woken = wakes(case)
    assert len(woken) == 1, f"At the reset time the gate must wake the orchestrator exactly once: {woken}"

    # The woken orchestrator works again.
    case.orch.event("Wake: the usage limit reset.", "Checking `fa gate status --json`; two lanes are idle.",
                    RESET_AT + timedelta(minutes=3, seconds=20))
    tick(case, capsys, RESET_AT + timedelta(minutes=5))
    report(case, capsys, RESET_AT + timedelta(minutes=6))  # This report may still tell the outage and its end.
    for at in (RESET_AT + timedelta(minutes=20), RESET_AT + timedelta(minutes=45)):
        tick(case, capsys, at)
    assert len(wakes(case)) == 1, "A working orchestrator must not be woken again"
    found = channels(case)
    still_open = [row for row in found["requests"]
                  if row.get("id") in alarms["requests"] and row.get("status") != "done"]
    still_open += [row for row in found["questions"]
                   if row.get("id") in alarms["questions"] and row.get("status") not in ("closed", "done")]
    assert not still_open, f"The alarm must clear once the orchestrator works again: {still_open}"
    out = report(case, capsys, RESET_AT + timedelta(minutes=50))
    assert not is_limit_alarm(out), f"A cleared alarm must not stay on the /fa report:\n{out}"

    # A cleared alarm re-arms: the next limit reply alarms the PM again, once.
    before = {name: len(items) for name, items in channels(case).items()}
    second = datetime(2026, 11, 6, 8, 2, tzinfo=UTC)  # 17:02 KST
    case.orch.event("Lane supply is low.", "You've hit your 5-hour limit · resets 9:40pm (Asia/Seoul)",
                    second, limited=True)
    tick(case, capsys, second + timedelta(minutes=1))
    tick(case, capsys, second + timedelta(minutes=6))
    after = {name: len(items) for name, items in channels(case).items()}
    added = {name: after[name] - before[name] for name in after if after[name] != before[name]}
    assert added and all(count == 1 for count in added.values()), (
        f"After the alarm cleared, a new usage-limit reply must alarm the PM once again: {added}"
    )
