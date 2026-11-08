"""Shared world for the bg-spare session identity tests (bug 11-09). Not a test module.

Claude may serve ``claude --bg --session-id <new id>`` from a pre-started
``claude bg-spare`` process. That process keeps its own earlier environment
(an inherited, stale ``FORAGENT_SESSION_ID``) and its own Claude session id,
which Claude Code exports to the session's commands as
``CLAUDE_CODE_SESSION_ID`` and uses as the transcript's file name
``<CLAUDE_CONFIG_DIR>/projects/<cwd with non-alphanumerics as '-'>/<id>.jsonl``.

The launch is faked (no real Claude process starts): ``orch_spawn_case``
records every command and returns success. A "session" here is only the
environment its commands run with; its commands go through the public CLI.
"""

from datetime import timedelta
import json
import os
from pathlib import Path

from test_orch_spawn import orch_spawn_case
from test_orch_review_handoff import bind_cli

# The pre-started process's own Claude session id (its transcript's name).
SPARE = "0b2c2a8b-5f1e-4d6a-9c3b-7e8f9a0b1c2d"


class World:
    def __init__(self, git_history, tmp_path, monkeypatch, capsys):
        from foragent import orch

        self.monkeypatch, self.capsys = monkeypatch, capsys
        self.config_dir = tmp_path / "claude-config"
        self.config_dir.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.config_dir))
        self.leave_session()  # The gate runs outside any Claude session.
        self.case, self.commands, self.pushes = orch_spawn_case(
            git_history, tmp_path / "world", monkeypatch)
        self.main = bind_cli(self.case, monkeypatch)
        monkeypatch.setenv("FORAGENT_VIEW", "off")
        self.polls = []

        def stop_after_first_poll(seconds):
            self.polls.append(self.case.clock[0])
            raise KeyboardInterrupt

        monkeypatch.setattr(orch.time, "sleep", stop_after_first_poll)

    # -- clock and sessions -------------------------------------------------
    @property
    def now(self):
        return self.case.clock[0]

    def advance(self, **delta):
        self.case.clock[0] += timedelta(**delta)

    def enter_session(self, *, foragent_id, claude_id):
        """Run the next commands as if typed inside a Claude session with this environment."""
        self.monkeypatch.setenv("FORAGENT_ROLE", "orchestrator")
        for name, value in (("FORAGENT_SESSION_ID", foragent_id), ("CLAUDE_CODE_SESSION_ID", claude_id)):
            if value is None:
                self.monkeypatch.delenv(name, raising=False)
            else:
                self.monkeypatch.setenv(name, value)

    def leave_session(self):
        for name in ("FORAGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "FORAGENT_ROLE"):
            self.monkeypatch.delenv(name, raising=False)

    # -- launches -------------------------------------------------------------
    def launched_ids(self):
        ids = []
        for argv in self.commands:
            if Path(argv[0]).name == "claude" and "--session-id" in argv:
                ids.append(argv[argv.index("--session-id") + 1])
        return ids

    def gate_launch(self):
        """One gate tick that opens an orchestrator; returns the id the launch recorded."""
        from foragent.scheduler import tick
        from test_orch_review_handoff import tick_context

        before = len(self.launched_ids())
        self.leave_session()
        tick(tick_context(self.case, start_runs=False))
        ids = self.launched_ids()
        assert len(ids) == before + 1, f"the gate tick must launch one orchestrator: {self.commands}"
        return ids[-1]

    def refresh_launch(self, note_text):
        """``fa orch handoff`` + ``fa orch refresh --ready`` from the given session; the new launch id."""
        note = self.case.root.parent / f"handoff-{len(self.commands)}.md"
        note.write_text(note_text, encoding="utf-8")
        before = len(self.launched_ids())
        self.fa_ok("orch", "handoff", "--file", str(note))
        self.fa_ok("orch", "refresh", "--ready")
        ids = self.launched_ids()
        assert len(ids) == before + 1, f"refresh --ready must launch one orchestrator: {self.commands}"
        return ids[-1]

    # -- public commands ------------------------------------------------------
    def fa(self, *args):
        self.capsys.readouterr()
        try:
            code = self.main([*map(str, args), "--repo-root", str(self.case.root)])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    def fa_ok(self, *args):
        code, out, err = self.fa(*args)
        assert code == 0, f"fa {' '.join(map(str, args))} must succeed (exit={code}):\n{out}{err}"
        return out

    def watch_first_poll(self):
        """``fa orch watch --follow`` in the current session, stopped right after its first poll."""
        polls = len(self.polls)
        code, out, err = self.fa("orch", "watch", "--follow")
        assert len(self.polls) == polls + 1, (
            f"the watch must complete its first poll (exit={code}):\n{out}{err}")
        return code

    def alive(self):
        return json.loads(self.fa_ok("orch", "alive", "--json"))

    def refresh_due(self):
        return json.loads(self.fa_ok("orch", "refresh-due", "--json"))

    def heartbeat(self):
        path = self.case.store.root / "orch-heartbeat.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    # -- transcripts ------------------------------------------------------------
    def transcript_file(self, session_id):
        root = str(self.case.root.resolve())
        project = "".join(char if char.isalnum() else "-" for char in root)
        return self.config_dir / "projects" / project / f"{session_id}.jsonl"

    def write_transcript(self, session_id, compactions):
        rows = [{"type": "user", "sessionId": session_id, "timestamp": self.now.isoformat(),
                 "message": {"role": "user", "content": "/fa-orchestrator"}}]
        for index in range(compactions):
            rows.append({"type": "system", "subtype": "compact_boundary", "sessionId": session_id,
                         "uuid": f"boundary-{index}", "timestamp": self.now.isoformat(),
                         "compactMetadata": {"trigger": "auto", "preTokens": 180000}})
        path = self.transcript_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path


def same_file(reported, expected: Path) -> bool:
    return isinstance(reported, str) and os.path.exists(reported) and os.path.samefile(reported, expected)


def spare_world(git_history, tmp_path, monkeypatch, capsys):
    """The bug's history, through gate launches only.

    The day before, the gate opened ``first``; it stayed silent, so three
    minutes later the gate opened ``second``, which retired ``first``. Today,
    with everything silent, the gate opens again and records ``recorded``, but
    Claude serves that launch from a spare process pre-started with
    ``FORAGENT_SESSION_ID=first`` (retired the day before) and its own Claude
    session id ``SPARE``. Returns ``(world, ids)``; the spare's watch has not run.
    """
    world = World(git_history, tmp_path, monkeypatch, capsys)
    world.advance(days=-1)
    first = world.gate_launch()
    world.advance(minutes=3)
    second = world.gate_launch()
    world.advance(days=1)
    recorded = world.gate_launch()
    ids = {"first": first, "second": second, "recorded": recorded}
    assert len(set(ids.values()) | {SPARE}) == 4, ids
    assert world.heartbeat().get("session_id") == recorded, "setup: the launch records its new id"
    return world, ids


def enter_spare(world, ids):
    """Commands typed in the session the bg-spare process actually opened."""
    world.enter_session(foragent_id=ids["first"], claude_id=SPARE)
