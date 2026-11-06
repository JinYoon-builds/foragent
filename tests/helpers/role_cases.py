"""Role-signal, tick-alert and request cases for unit tests of free-text options (copied from the locked tests that define them)."""

from copy import deepcopy
from datetime import timedelta
import json
import pytest
import socket
import traceback
import yaml

from helpers.home import cli_exit
from helpers.local_cli import offline_local_cli
from helpers.review_case import bind_cli, make_case, tick_context


DEFAULTS = ("orchestrator", "architect", "docs")


def role_entry(name, *, signals=()):
    return {"agent": f"fa-{name}", "skills": [f"fa-{name}"],
            "memory": f".claude/agent-memory/fa-{name}/", "signals": deepcopy(list(signals))}


def write_role_snapshot(root, roles):
    """Consumer fixtures, not an alternative role writer in the product.

    Like the existing phase-event fixtures, seed the documented source and its
    projections so consumers can be tested independently of fa init / fa role.
    """
    path = root / ".foragent.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["roles"] = deepcopy(roles)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    for name, entry in roles.items():
        agent = root / ".claude/agents" / f"{entry['agent']}.md"
        agent.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"name": entry["agent"], "memory": "project", "skills": entry["skills"]}
        agent.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False)
                         + f"---\nFollow the {name} role skill.\n", encoding="utf-8")
        memory = root / entry["memory"] / "MEMORY.md"
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text(f"# {name} project memory\n", encoding="utf-8")
        for skill in entry["skills"]:
            existing_skill(root, skill)


def existing_skill(root, name):
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Offline role skill\n---\n"
                    "Propose changes through fa orch request; the orchestrator decides.\n",
                    encoding="utf-8")
    (path.parent / "LICENSE").write_text("SPDX-License-Identifier: MIT\n", encoding="utf-8")


def notify_blocked_capture(monkeypatch):
    from foragent import notify

    pushes = []

    def push(config, title, line, link=None):
        pushes.append({"title": title, "line": line, "link": link})
        return True

    monkeypatch.setattr(notify, "send", push)
    return pushes


@pytest.fixture
def tick_case(git_history, tmp_path, monkeypatch, isolated_home):
    from foragent import cli, scheduler
    from foragent.config import load_config

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[])
    # An idle repository keeps unrelated runner, stall and tidy alerts out of the test.
    case.store.finish_run(1, outcome="cancelled")
    case.tracker.close_issue(1)
    case.main = bind_cli(case, monkeypatch)
    (case.root / ".foragent.yml").write_text(yaml.safe_dump({
        "repo": "owner/repo", "skill_libraries": [], "notify": "claude-push",
    }), encoding="utf-8")
    # Exercise the real config loader, including the new threshold's YAML path.
    monkeypatch.setattr(cli, "load_config", load_config)
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    case.pushes = notify_blocked_capture(monkeypatch)
    case.failure = None
    case.tick_calls = []
    recover = scheduler._recover_runs

    def recovery_step(ctx, report):
        case.tick_calls.append(ctx.now())
        if case.failure is not None:
            raise case.failure
        return recover(ctx, report)

    def unexpected(*args, **kwargs):
        pytest.fail("Offline tick acceptance tests must not launch workers or access the network")

    def context(**kwargs):
        case.ctx.config = kwargs["config"]
        return tick_context(case, start_runs=kwargs["start_runs"], baseline_launcher=unexpected)

    monkeypatch.setattr(cli, "TickContext", context)
    monkeypatch.setattr(scheduler, "_recover_runs", recovery_step)
    monkeypatch.setattr(cli, "spawn_runner", lambda *args: unexpected)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: None)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)
    return case


def run_tick(case, capsys, failure=None):
    """Run the real fa entry point, accepting propagated or printed tracebacks.

    A fresh StateStore on every invocation models launchd's separate processes;
    no test writes outcomes, requests, traceback logs or alert suppression state.
    """
    from foragent.state import StateStore

    case.clock[0] += timedelta(minutes=5)
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    case.failure = failure
    calls_before = len(case.tick_calls)
    capsys.readouterr()
    escaped_traceback = ""
    try:
        code = cli_exit(case.main, ["gate", "tick", "--no-start", "--json",
                                   "--repo-root", str(case.root)])
    except Exception as exc:
        if failure is None:
            raise
        assert type(exc) is type(failure) and str(exc) == str(failure), (
            "fa gate tick must load its config and reach the injected failure; "
            f"instead raised {type(exc).__name__}: {exc}"
        )
        code = 1
        escaped_traceback = "".join(traceback.format_exception(exc))
    output = capsys.readouterr()
    assert len(case.tick_calls) == calls_before + 1, (
        f"fa gate tick must load its config and reach scheduler recovery; CLI stderr: {output.err}"
    )
    if failure is None:
        assert code == 0, output.err
    else:
        assert code not in (None, 0), "A crashing gate tick must still exit nonzero"
        diagnostic = escaped_traceback + output.err
        assert "Traceback (most recent call last):" in diagnostic, "The CLI must preserve the traceback"
        assert f"{type(failure).__name__}: {failure}" in diagnostic


def inbox(case, capsys):
    capsys.readouterr()
    assert cli_exit(case.main, ["orch", "inbox", "--json", "--repo-root", str(case.root)]) == 0
    return json.loads(capsys.readouterr().out)


IDLE = "idle_lanes_with_backlog"


OTHER = "hot_overlap_files"


CAP = {"kind": "cap", "normal": False, "detail": "no free parallel slot for #41"}


IDLE_SIGNAL = {"paths": [], "kinds": [IDLE]}


OTHER_SIGNAL = {"paths": [], "kinds": [OTHER]}


@pytest.fixture
def not_now_case(tick_case, monkeypatch):
    from foragent import orch_facts

    case = tick_case
    roles = {name: role_entry(name) for name in DEFAULTS}
    roles["architect"]["signals"] = [IDLE_SIGNAL, OTHER_SIGNAL]
    write_role_snapshot(case.root, roles)
    case.facts = {
        "kinds": [IDLE],
        "kind_numbers": {
            IDLE: {"idle_lanes": ["order", "view"], "ready_by_lane": {"orch": 4}},
        },
        "binding": deepcopy(CAP),
    }

    def measured_facts(*args, **kwargs):
        # Time advances on every real CLI tick. Observation time is not a change
        # to the binding constraint, idle lanes or their ready work.
        snapshot = deepcopy(case.facts)
        snapshot["at"] = case.clock[0].isoformat()
        return snapshot

    monkeypatch.setattr(orch_facts, "collect", measured_facts)
    return case


def tick_signals(case, capsys):
    run_tick(case, capsys)
    rows = [row for row in inbox(case, capsys) if row.get("role") == "architect"]
    assert case.pushes == [], "Role signals stay in the orchestrator inbox"
    return rows


@pytest.fixture
def request_case(tmp_path, monkeypatch, isolated_home):
    import socket
    import subprocess

    case = offline_local_cli(tmp_path, monkeypatch)
    write_role_snapshot(case.root, {name: role_entry(name) for name in DEFAULTS})

    def unexpected(*args, **kwargs):
        pytest.fail("Request completion and scorecard reads must stay local")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)
    monkeypatch.setattr(socket, "create_connection", unexpected)
    return case
