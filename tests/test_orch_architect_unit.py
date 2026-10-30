"""Boundary tests for the architect session record, liveness and executor options."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foragent import orch_architect, processes
from foragent.config import Config
from foragent.executors import codex_command
from foragent.processes import ProcessIdentity
from foragent.state import StateStore


def identity(pid, start):
    return ProcessIdentity(pid, 1, pid, pid, start)


def test_session_alive_matches_start_time_when_recorded(monkeypatch):
    monkeypatch.setattr(processes, "read_process_identity", lambda pid: identity(pid, 10.0))
    assert orch_architect.session_alive({"pid": 4242, "start_time": 10.0})
    assert not orch_architect.session_alive({"pid": 4242, "start_time": 11.0}), "A reused pid is not the architect"


def test_session_alive_falls_back_to_signal_zero_probe(monkeypatch):
    def unreadable(pid):
        raise PermissionError(pid)

    probes = []

    def probe(pid, sig):
        probes.append((pid, sig))
        raise ProcessLookupError(pid)

    monkeypatch.setattr(processes, "read_process_identity", unreadable)
    monkeypatch.setattr(orch_architect.os, "kill", probe)
    assert not orch_architect.session_alive({"pid": 4242, "start_time": 10.0})
    assert not orch_architect.session_alive({"pid": 4242, "start_time": None})
    assert probes == [(4242, 0), (4242, 0)]
    assert not orch_architect.session_alive(None)
    assert not orch_architect.session_alive({"pid": "4242"})


def test_call_refuses_while_the_executor_is_paused(tmp_path):
    store = StateStore(tmp_path / "state")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    store.pause_executor("claude", now + timedelta(hours=1))
    with pytest.raises(orch_architect.ArchitectRefused, match="paused"):
        orch_architect.call(tmp_path, store, Config(executor="claude"), "evidence", now)
    assert not orch_architect.record_path(store).exists()



def test_call_refuses_a_multi_line_short_before_launching(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(orch_architect.subprocess, "Popen", lambda *a, **kw: launches.append(a))
    store = StateStore(tmp_path / "state")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    with pytest.raises(orch_architect.ArchitectRefused, match="one line"):
        orch_architect.call(tmp_path, store, Config(executor="claude"), "evidence", now,
                            short="first\nsecond")
    assert launches == [] and not orch_architect.record_path(store).exists()
    assert not (store.root / "orch-events.jsonl").exists()


def test_call_refuses_a_role_missing_from_the_roles_source(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(orch_architect.subprocess, "Popen", lambda *a, **kw: launches.append(a))
    store = StateStore(tmp_path / "state")
    now = datetime(2026, 10, 27, tzinfo=UTC)
    with pytest.raises(orch_architect.ArchitectRefused, match="security"):
        orch_architect.call(tmp_path, store, Config(executor="claude"), "evidence", now, "security")
    assert launches == [] and not orch_architect.record_path(store, "security").exists()
    assert not (store.root / "orch-events.jsonl").exists()


def test_generic_role_prompt_names_its_memory_and_codex_gets_its_skill_text(tmp_path):
    entry = {"agent": "fa-security", "skills": ["fa-security"], "memory": ".claude/agent-memory/fa-security/"}
    skill = tmp_path / "skills/fa-security/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("check secrets\n")
    prompt = orch_architect.build_prompt("why", tmp_path, "codex", "security", entry)
    assert "check secrets" in prompt and ".claude/agent-memory/fa-security/MEMORY.md" in prompt
    assert "fa orch request --from security" in prompt
    assert "check secrets" not in orch_architect.build_prompt("why", tmp_path, "claude", "security", entry)

def test_session_options_use_tier_l_and_codex_only_gets_the_state_root(tmp_path):
    store = StateStore(tmp_path / "state")
    codex = orch_architect.session_options(Config(executor="codex"), store)
    assert codex["effort"] == Config().tiers["l"]["codex_effort"] and codex["add_dirs"] == (store.root,)
    claude = orch_architect.session_options(Config(executor="claude"), store)
    assert "add_dirs" not in claude and "effort" not in claude


def test_codex_command_adds_extra_writable_roots():
    argv = codex_command(Path("/repo"), Path("/out"), add_dirs=(Path("/state"),))
    assert argv[argv.index("--add-dir") + 1] == "/state"
    assert "--add-dir" not in codex_command(Path("/repo"), Path("/out"))


def test_run_architect_is_hidden_from_gate_help_usage_and_errors(capsys):
    from foragent import cli

    with pytest.raises(SystemExit):
        cli._parser().parse_args(["gate", "--help"])
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["gate", "no-such-verb"])
    output = capsys.readouterr()
    assert "run-issue" in output.out and "check-done" in output.err
    assert "run-architect" not in output.out + output.err and "SUPPRESS" not in output.out
    args = cli._parser().parse_args(["gate", "run-architect", "--repo-root", ".",
                                     "--started-at", "2026-10-27T00:00:00+00:00", "--why", "w"])
    assert args.action == "run-architect" and callable(args.handler)


@pytest.mark.parametrize("executor", ["claude", "codex"])
def test_run_session_keeps_the_final_message_in_log_and_event(tmp_path, monkeypatch, executor):
    import json

    from foragent import executors
    from foragent.executors import ExecResult

    store = StateStore(tmp_path / "state")
    started_at = "2026-10-27T01:02:03+00:00"
    calls = []

    class Fake:
        def run(self, **kwargs):
            calls.append(kwargs)
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            return ExecResult(0, False, "Nothing to change.\nfacts showed no hot file", kwargs["log_path"])

    monkeypatch.setattr(executors, "executor_for", lambda name: Fake())
    now = lambda: datetime(2026, 10, 27, 2, tzinfo=UTC)
    assert orch_architect.run_session(tmp_path, store, Config(executor=executor), "why", started_at, now) == 0
    log = orch_architect.log_path(store, started_at)
    assert "Nothing to change." in log.read_text(encoding="utf-8")
    event = json.loads((store.root / "orch-events.jsonl").read_text().splitlines()[-1])
    assert event["kind"] == "architect-done" and event["summary"] == "exit code 0: Nothing to change."
    assert event["message"].startswith("Nothing to change.")
    expected = orch_architect.events_path(store, started_at) if executor == "codex" else None
    assert calls[0].get("events_path") == expected


def test_codex_executor_keeps_events_at_a_named_path(tmp_path, monkeypatch):
    from foragent import executors

    events = tmp_path / "architect.stdout"

    def fake_run_logged(argv, *, stdout_path, **kwargs):
        stdout_path.write_text('{"type":"thread.started","thread_id":"t1"}\n')
        return 0, False, None

    monkeypatch.setattr(executors, "run_logged", fake_run_logged)
    executors.CodexExecutor().run(worktree=tmp_path, prompt="p", model=None, timeout_s=1,
                                  log_path=tmp_path / "log", events_path=events)
    assert "thread.started" in events.read_text()


def _project(tmp_path, *, agent=False, skill=None, memory=False):
    root = tmp_path / "project"
    root.mkdir()
    if agent:
        (root / ".claude/agents").mkdir(parents=True)
        (root / Path(".claude/agents/fa-architect.md")).write_text("---\nname: fa-architect\n---\n")
    if skill is not None:
        path = root / skill
        path.parent.mkdir(parents=True)
        path.write_text(f"skill text from {skill}\n")
    if memory:
        (root / Path(".claude/agent-memory/fa-architect/MEMORY.md")).parent.mkdir(parents=True)
    return root


def test_claude_architect_names_its_agent_only_when_the_project_has_it(tmp_path):
    store = StateStore(tmp_path / "state")
    plain = _project(tmp_path)
    assert "agent" not in orch_architect.session_options(Config(executor="claude"), store, plain)
    plain.rename(tmp_path / "old")
    root = _project(tmp_path, agent=True)
    assert orch_architect.session_options(Config(executor="claude"), store, root)["agent"] == "fa-architect"
    assert "Follow your fa-architect skill" in orch_architect.build_prompt("why", root, "claude")


def test_codex_architect_writes_memory_and_reads_the_project_skill_first(tmp_path):
    store = StateStore(tmp_path / "state")
    root = _project(tmp_path, skill=".claude/skills/fa-architect/SKILL.md", memory=True)
    (root / "skills/fa-architect/SKILL.md").parent.mkdir(parents=True)
    (root / "skills/fa-architect/SKILL.md").write_text("packaged copy\n")
    options = orch_architect.session_options(Config(executor="codex"), store, root)
    assert options["add_dirs"] == (store.root, root / ".claude/agent-memory/fa-architect")
    prompt = orch_architect.build_prompt("why", root, "codex")
    assert "skill text from .claude/skills/fa-architect/SKILL.md" in prompt and "packaged copy" not in prompt


def test_codex_architect_without_a_skill_keeps_only_the_pointer(tmp_path):
    root = _project(tmp_path)
    prompt = orch_architect.build_prompt("why", root, "codex")
    assert "skills/fa-architect/SKILL.md" in prompt and orch_architect.REFACTOR in prompt
    assert "one proposal per file or concern" not in prompt.casefold()


def test_role_skills_contain_no_symlinks_so_skill_install_accepts_them():
    root = Path(__file__).resolve().parents[1] / "skills"
    for name in ("fa-architect", "fa-orchestrator"):
        assert not [path for path in (root / name).rglob("*") if path.is_symlink()], name


@pytest.mark.parametrize("executor", ("claude", "codex"))
def test_architect_session_reads_agent_skills_and_memory_from_the_roles_source(tmp_path, monkeypatch, executor):
    """fa role set architect --skill custom-audit must reach the session, not a hardcoded fa-architect."""
    from foragent import executors
    from foragent.executors import ExecResult

    root = tmp_path / "project"
    for path, body in ((".claude/agents/fa-audit.md", "---\nname: fa-audit\n---\n"),
                       (".claude/agents/fa-architect.md", "---\nname: fa-architect\n---\n"),
                       ("skills/custom-audit/SKILL.md", "custom audit procedure\n"),
                       ("skills/fa-architect/SKILL.md", "default architect procedure\n"),
                       ("mem/audit/MEMORY.md", "")):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(body)
    roles = {name: {"agent": f"fa-{name}", "skills": [f"fa-{name}"], "memory": f".claude/agent-memory/fa-{name}/",
                    "signals": []} for name in ("orchestrator", "docs")}
    roles["architect"] = {"agent": "fa-audit", "skills": ["custom-audit"], "memory": "mem/audit/", "signals": []}
    calls = []

    class Fake:
        def run(self, **kwargs):
            calls.append(kwargs)
            kwargs["log_path"].parent.mkdir(parents=True, exist_ok=True)
            return ExecResult(0, False, "Nothing to change.", kwargs["log_path"])

    monkeypatch.setattr(executors, "executor_for", lambda name: Fake())
    store = StateStore(tmp_path / "state")
    started_at = datetime(2026, 10, 27, tzinfo=UTC).isoformat()
    config = Config(executor=executor, roles=roles)
    assert orch_architect.run_session(root, store, config, "why", started_at,
                                      lambda: datetime(2026, 10, 27, 1, tzinfo=UTC)) == 0
    turn, = calls
    prompt = turn["prompt"]
    assert "custom-audit" in prompt and "fa-architect skill" not in prompt
    assert "mem/audit/MEMORY.md" in prompt and "agent-memory/fa-architect" not in prompt
    if executor == "claude":
        assert turn["agent"] == "fa-audit"
    else:
        assert "custom audit procedure" in prompt and "default architect procedure" not in prompt
        assert turn["add_dirs"] == (store.root, root / "mem/audit")
