"""Acceptance test for usage-recheck. Locked by Foragent; do not edit.

Contracts added to #105: usage_recheck_minutes defaults to 10; Claude's account
snapshot is $XDG_DATA_HOME/foragent/claude-rate-limits.json, containing ISO UTC
seen_at and the unchanged statusline rate_limits object. A snapshot older than
twice the interval is stale. Codex's live JSON-RPC reply has no cache timestamp.
The existing orch-events.jsonl envelope uses kind=quota-recovered and names the
executor and source (codex-app-server / claude-statusline) in its summary.
No new exhaustion flag is invented: usage_case drives the public runner as #105
does. All launches, model adapters and process signals are harmless test doubles.
"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from test_orch_review_handoff import tick_context
from test_skills_autosync import isolated_home
from test_usage_read import CLAUDE_RESET, CODEX_RESET, usage_case


@pytest.fixture(autouse=True)
def free_sources_only(monkeypatch, isolated_home):
    """Fail before any real adapter or signal can run, including signal zero."""
    from foragent import executors, scheduler

    def forbidden(*args, **kwargs):
        pytest.fail("A usage recheck must never run a model or signal a process")

    monkeypatch.setattr(executors.CodexExecutor, "run", forbidden)
    monkeypatch.setattr(executors.ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(scheduler, "machine_load", lambda: ("normal", 0.1))
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_home / ".claude"))


@pytest.fixture
def codex_server(tmp_path, monkeypatch):
    """A real, local stdio subprocess; fake only the executable/file boundary.

    It understands the optional initialize/initialized handshake, not threads or
    turns. Changing the reply file also works if the client keeps one server alive.
    """
    root = tmp_path / "fake-codex"
    root.mkdir()
    reply, traffic = root / "reply.json", root / "traffic.jsonl"
    script = root / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"reply = Path({str(reply)!r})\ntraffic = Path({str(traffic)!r})\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    with traffic.open('a') as stream:\n"
        "        stream.write(json.dumps({'request': request}) + '\\n')\n"
        "    method = request.get('method')\n"
        "    if method == 'initialized':\n"
        "        continue\n"
        "    if method == 'initialize':\n"
        "        response = {'result': {'userAgent': 'offline-acceptance'}}\n"
        "    elif method == 'account/rateLimits/read':\n"
        "        response = json.loads(reply.read_text())\n"
        "    else:\n"
        "        response = {'error': {'code': -32601, 'message': 'No model turns allowed'}}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], **response}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(root) + os.pathsep + os.environ["PATH"])
    popen = subprocess.Popen
    boundary = SimpleNamespace(unavailable=False)

    def launch(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and Path(argv[0]).name in {"codex", "claude"}:
            with traffic.open("a") as stream:
                stream.write(json.dumps({"argv": [str(arg) for arg in argv]}) + "\n")
            assert Path(argv[0]).name == "codex" and list(argv[1:2]) == ["app-server"], (
                "Only codex app-server is free; exec and claude -p are forbidden", argv)
            if boundary.unavailable:
                raise FileNotFoundError("offline codex is unavailable")
            argv = [sys.executable, str(script), *argv[1:]]
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)

    def records():
        return [json.loads(line) for line in traffic.read_text().splitlines()] if traffic.exists() else []

    def reads():
        return [row for row in records() if row.get("request", {}).get("method") == "account/rateLimits/read"]

    def respond(primary=23, secondary=2, *, reset=CODEX_RESET, error=False):
        value = {"error": {"code": -32000, "message": "offline rate limits unavailable"}} if error else {
            "result": {"rateLimits": {
                "primary": {"usedPercent": primary, "windowDurationMins": 300, "resetsAt": reset},
                "secondary": {"usedPercent": secondary, "windowDurationMins": 10080, "resetsAt": reset},
            }},
        }
        reply.write_text(json.dumps(value), encoding="utf-8")

    respond()
    boundary.respond, boundary.records, boundary.reads = respond, records, reads
    return boundary


def claude_snapshot(case, primary=23, secondary=2, *, seen_at=None, reset=CLAUDE_RESET):
    path = Path(os.environ["XDG_DATA_HOME"]) / "foragent/claude-rate-limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "seen_at": (seen_at or case.clock[0]).isoformat(),
        "rate_limits": {
            "five_hour": {"used_percentage": primary, "resets_at": reset},
            "seven_day": {"used_percentage": secondary, "resets_at": reset},
        },
    }), encoding="utf-8")
    return path


def exhausted_case(git_history, tmp_path, monkeypatch, *, executor="codex", **kwargs):
    case = usage_case(git_history, tmp_path, monkeypatch, executor=executor,
                      used_percent=kwargs.pop("used_percent", 100), limit=True, **kwargs)
    case.started = case.clock[0]
    case.until = datetime.fromtimestamp(CODEX_RESET if executor == "codex" else CLAUDE_RESET, UTC)
    case.lane = case.store.get_run(1).lane
    # make_case already saves the approval; make it observable to the scheduler too.
    case.tracker.record_label(1, "approved", "human")
    case.launched = []
    case.gate = tick_context(case)
    # A recorded launch is sufficient: never create a runner or give it a PID.
    case.gate.launcher = lambda number: case.launched.append(number)
    case.gate.kill_runner = lambda pid: pytest.fail("The gate must not signal any runner")
    return case


def recovered_events(case):
    path = case.store.root / "orch-events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row["kind"] == "quota-recovered"]


def assert_recovered(case, executor):
    assert not case.store.lane_paused(case.lane, case.clock[0]), "fresh free usage must clear the quota pause"
    events = recovered_events(case)
    assert len(events) == 1, "one exhausted-to-available transition must record one quota-recovered event"
    assert executor in events[0]["summary"]
    assert {"codex": "codex-app-server", "claude": "claude-statusline"}[executor] in events[0]["summary"]
    assert datetime.fromisoformat(events[0]["at"]) == case.clock[0]


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_usage_recheck_recovers_before_announced_reset_and_launches_without_a_model_turn(
    git_history, tmp_path, monkeypatch, codex_server, executor,
):
    from foragent.scheduler import tick

    case = exhausted_case(git_history, tmp_path, monkeypatch, executor=executor)
    case.clock[0] += timedelta(minutes=10)
    if executor == "claude":
        claude_snapshot(case)
    assert case.clock[0] < case.until
    tick(case.gate)
    assert_recovered(case, executor)
    # Recovery may dispatch this tick or the next, but never needs human resume.
    if not case.launched:
        tick(case.gate)
    assert case.launched == [1], "the recovered executor must accept the waiting issue"
    case.gate.start_runs = False
    tick(case.gate)
    assert len(recovered_events(case)) == 1, "idle ticks must not duplicate recovery events"
    if executor == "codex":
        assert codex_server.reads(), "recovery must actually read the free JSON-RPC source"
        assert all(row["argv"][1] == "app-server" for row in codex_server.records() if "argv" in row)
        assert all(row["request"]["method"] in {"initialize", "initialized", "account/rateLimits/read"}
                   for row in codex_server.records() if "request" in row)
    else:
        assert codex_server.records() == [], "Claude usage comes from its file, without a Codex process"


@pytest.mark.parametrize("configured", [None, 3], ids=["default-10", "configured-3"])
def test_usage_recheck_honors_default_and_configured_interval(
    git_history, tmp_path, monkeypatch, codex_server, configured,
):
    from foragent.config import load_config
    from foragent.scheduler import tick

    case = exhausted_case(git_history, tmp_path, monkeypatch)
    # The public YAML loader requires an unset ceiling to be omitted, not null.
    values = {key: value for key, value in asdict(case.config).items()
              if key != "max_parallel" or value is not None}
    if configured is not None:
        values["usage_recheck_minutes"] = configured
    (case.root / ".foragent.yml").write_text(yaml.safe_dump(values), encoding="utf-8")
    config = load_config(case.root)
    interval = 10 if configured is None else configured
    assert getattr(config, "usage_recheck_minutes", None) == interval, "usage_recheck_minutes must default to 10"
    case.gate.config = config
    case.gate.start_runs = False
    codex_server.respond(primary=100)
    case.clock[0] = case.started + timedelta(minutes=interval)
    tick(case.gate)
    assert len(codex_server.reads()) == 1, "an exhausted executor needs a free recheck"
    codex_server.respond(primary=23)
    case.clock[0] += timedelta(minutes=interval, seconds=-1)
    tick(case.gate)
    assert len(codex_server.reads()) == 1, "ticks must not poll more often than the configured interval"
    assert case.store.lane_paused(case.lane, case.clock[0])
    assert recovered_events(case) == []
    case.clock[0] += timedelta(seconds=1)
    tick(case.gate)
    assert len(codex_server.reads()) == 2
    assert_recovered(case, "codex")


@pytest.mark.parametrize("executor,fault", [
    ("codex", "unavailable"), ("codex", "rpc-error"),
    ("claude", "missing"), ("claude", "malformed"), ("claude", "stale"),
])
def test_usage_recheck_unavailable_source_keeps_announced_reset_as_upper_bound(
    git_history, tmp_path, monkeypatch, codex_server, executor, fault,
):
    from foragent.scheduler import tick

    case = exhausted_case(git_history, tmp_path, monkeypatch, executor=executor)
    if executor == "claude":
        # Codex is at its limit too, so no quota mode can move the run to it (#107):
        # the lane must wait for the announced Claude reset.
        case.store.pause_executor("codex", case.until)
        codex_server.unavailable = True  # keep that Codex pause in place
    case.clock[0] += timedelta(minutes=10)
    if executor == "codex":
        codex_server.unavailable = fault == "unavailable"
        codex_server.respond(error=True)
    elif fault != "missing":
        # A fresh filesystem mtime must not disguise an old observation.
        path = claude_snapshot(case, seen_at=case.clock[0] - timedelta(minutes=20, seconds=1))
        if fault == "malformed":
            path.write_text("{not-json", encoding="utf-8")
    tick(case.gate)
    assert case.launched == [] and recovered_events(case) == []
    assert case.store.lane_paused(case.lane, case.until - timedelta(seconds=1)), (
        "missing, failing or stale free usage must preserve the announced reset")
    assert not case.store.lane_paused(case.lane, case.until), "free-source failures must never extend the reset"
    case.clock[0] = case.until - timedelta(seconds=1)
    tick(case.gate)
    assert case.launched == []
    case.clock[0] = case.until
    tick(case.gate)
    if not case.launched:
        tick(case.gate)
    assert case.launched == [1], "the announced reset must release work even when the free source stays broken"


def test_usage_recheck_without_known_reset_uses_limit_wait_minutes(
    git_history, tmp_path, monkeypatch, codex_server,
):
    from foragent.scheduler import tick

    case = exhausted_case(git_history, tmp_path, monkeypatch, unreadable=True)
    until = case.started + timedelta(minutes=17)  # usage_case's explicit limit_wait_minutes.
    codex_server.respond(error=True)
    case.clock[0] += timedelta(minutes=10)
    tick(case.gate)
    assert codex_server.reads(), "even a limit without a parsed reset needs the free recheck"
    assert case.launched == [] and recovered_events(case) == []
    assert case.store.lane_paused(case.lane, until - timedelta(seconds=1))
    assert not case.store.lane_paused(case.lane, until)
    case.clock[0] = until
    tick(case.gate)
    if not case.launched:
        tick(case.gate)
    assert case.launched == [1], "free-source failures must not restart the fallback wait on every tick"


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("primary,secondary", [(100, 2), (23, 100)], ids=["primary-full", "secondary-full"])
def test_usage_recheck_waits_for_every_exhausted_window(
    git_history, tmp_path, monkeypatch, codex_server, executor, primary, secondary,
):
    from foragent.scheduler import tick

    case = exhausted_case(git_history, tmp_path, monkeypatch, executor=executor)
    case.gate.start_runs = False
    case.clock[0] += timedelta(minutes=10)
    if executor == "codex":
        codex_server.respond(primary, secondary)
    else:
        claude_snapshot(case, primary, secondary)
    tick(case.gate)
    assert case.store.lane_paused(case.lane, case.clock[0])
    assert recovered_events(case) == [], "one available window does not recover an exhausted executor"
    case.clock[0] += timedelta(minutes=10)
    if executor == "codex":
        codex_server.respond()
    else:
        claude_snapshot(case)
    tick(case.gate)
    assert_recovered(case, executor)


def test_usage_recheck_higher_usage_does_not_shorten_pause(
    git_history, tmp_path, monkeypatch, codex_server,
):
    from foragent.scheduler import tick

    # #105 explicitly treats Claude's rejected status as exhaustion even at 60%.
    case = exhausted_case(git_history, tmp_path, monkeypatch, executor="claude",
                          used_percent=60, status="rejected")
    case.gate.start_runs = False
    case.clock[0] += timedelta(minutes=10)
    claude_snapshot(case, primary=90, reset=int((case.clock[0] + timedelta(minutes=1)).timestamp()))
    tick(case.gate)
    assert recovered_events(case) == []
    assert case.store.lane_paused(case.lane, case.until - timedelta(seconds=1)), (
        "higher usage and an earlier advertised reset must not shorten the original pause")
    assert not case.store.lane_paused(case.lane, case.until)
