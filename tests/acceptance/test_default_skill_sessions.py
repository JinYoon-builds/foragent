"""Role-scoped default skills through public configuration and session inputs.

The fixture serializer supplies an example public role table. Assertions concern
executor-visible results, never the parsed setting's internal representation.
All skill names are arbitrary. No selection or semantic judgment is replaced.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from foragent.config import load_config
from foragent.executors import ExecResult
from test_init import setup  # noqa: F401
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home, write_global_config  # noqa: F401

ROLE_NAMES = ("workers", "reviewers", "orchestrator", "architect", "docs")
ROLE_SKILLS = {role: f"fixture-role-{n}-q7" for n, role in enumerate(ROLE_NAMES)}
CHOSEN = ROLE_SKILLS["workers"]
OTHER = "fixture-unassigned-t8"


def marker(name):
    return f"FIXTURE-CONTENT::{name}::GUIDANCE"


def assignments():
    return {role: [name] for role, name in ROLE_SKILLS.items()}


def setting_values(table):
    """Keep fixture input serialization separate from the behavior assertions."""
    return {} if table is None else {"default_skills": {role: list(names) for role, names in table.items()}}


def skill_files(names=None, license="MIT"):
    names = tuple(ROLE_SKILLS.values()) if names is None else names
    return {path: content for name in names for path, content in {
        f"skills/{name}/SKILL.md": f"---\nname: {name}\ndescription: Fixture guidance\n---\n{marker(name)}\n",
        f"skills/{name}/LICENSE": f"SPDX-License-Identifier: {license}\n",
    }.items()}


def write_skills(root, **options):
    for name, content in skill_files(**options).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def configure(root, table, **values):
    path = root / ".foragent.yml"
    current = yaml.safe_load(path.read_text()) if path.exists() else {}
    current.update(repo="owner/repo", skill_libraries=[])
    current.update(values)
    current.pop("default_skills", None)
    current.update(setting_values(table))
    path.write_text(yaml.safe_dump(current))
    return load_config(root)


def received(call, sandbox):
    """Follow only files named by executor input or its selected native agent."""
    root = call["worktree"]
    result = "\n".join(value for value in call.values() if isinstance(value, str))
    agent = call.get("agent")
    if agent:
        path = root / ".claude/agents" / f"{agent}.md"
        if path.is_file():
            value = path.read_text()
            result += "\n" + value
            metadata = yaml.safe_load(value.split("---", 2)[1])
            for name in metadata.get("skills", []):
                for base in (root / ".claude/skills", root / "skills"):
                    if (path := base / name / "SKILL.md").is_file():
                        result += "\n" + path.read_text()
    paths = [path for path in sandbox.rglob("*") if path.is_file() and ".git" not in path.parts]
    seen = set()
    while True:
        changed = False
        for path in paths:
            refs = [str(path)]
            if path.is_relative_to(root):
                refs.append(path.relative_to(root).as_posix())
            if path not in seen and any(ref in result for ref in refs):
                seen.add(path)
                try:
                    result += "\n" + path.read_text()
                except (UnicodeError, OSError):
                    continue
                changed = True
        if not changed:
            return result


def assert_role_input(value, role, table):
    expected = set((table or {}).get(role, ()))
    for assigned_role, name in ROLE_SKILLS.items():
        if name in expected:
            assert name in value and marker(name) in value
        else:
            assert name not in value and marker(name) not in value, (role, assigned_role, name)


def configured_case(git_history, tmp_path, monkeypatch, executor="codex", lane="api", table=None):
    names = set(ROLE_SKILLS.values()) | {OTHER}
    for values in (table or {}).values():
        names.update(values)
    git_history.update(skill_files(sorted(names)))
    case = make_case(git_history, tmp_path, monkeypatch, executor=executor,
                     reviewer="claude" if executor == "codex" else "codex", skill_libraries=[],
                     lanes={"api": {"paths": ["feature.txt"], "skills": []}})
    case.config = case.ctx.config = configure(case.root, table, executor=executor,
        reviewer=case.config.reviewer, review="fast", approvers=["human"], lanes=case.config.lanes)
    case.store.update_run(1, lane=lane)
    case.reviewer.run = lambda **call: ExecResult(0, False,
        json.dumps({"result": "(none)"}) if call.get("review_level") is not None else "VERDICT: PASS",
        call["log_path"])
    return case


def run_consumer(consumer, executor, history, root, mp, table, observe=None, available=()):
    """Execute existing public paths; do not replace session routing."""
    from foragent import capacity
    from foragent.runner import run_issue
    # Admission is not under test: supply stable external sensor facts.
    mp.setattr(capacity, "machine_load", lambda: ("normal", 0.5))
    mp.setattr(capacity, "kernel_pressure", lambda: "normal")
    observe = observe or (lambda call: received(call, root))
    installed = set(ROLE_SKILLS.values()) | set(available)
    for names in (table or {}).values(): installed.update(names)
    if consumer in {"workers", "no-lane"}:
        case = configured_case(history, root, mp, executor, lane="api" if consumer == "workers" else "main", table=table)
        seen = []
        case.implementer.steps = [lambda call: (seen.append(observe(call)), case.implement(call))[1]]
        assert run_issue(1, case.ctx) == "merged"
        return seen
    if consumer == "reviewers":
        from test_review_perspective_fanout import direction_case, is_lane_turn
        history.update(skill_files(sorted(installed)))
        case = direction_case(history, root, mp, reviewer=executor)
        case.config = case.ctx.config = configure(case.root, table,
            executor=case.config.executor, reviewer=executor, review="fast", approvers=["human"],
            review_perspectives=case.config.review_perspectives, review_reviewers=1,
            test_command="true", max_retries=1, review_max=1)
        seen = []
        def execute(**call):
            if is_lane_turn(call):
                return case.implementer.run(**call)
            seen.append(observe(call))
            return ExecResult(0, False, "VERDICT: PASS", call["log_path"])
        case.ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
        assert run_issue(1, case.ctx) == "merged" and len(seen) >= 6
        return seen
    if consumer in {"docs", "architect"}:
        from test_orch_architect import architect_case, finish_session
        from test_orch_inbox import inbox_command
        case = architect_case(root, mp)
        write_skills(case.root, names=sorted(installed))
        configure(case.root, table, executor=executor, roles={consumer: {
            "agent": "fixture-agent", "skills": [], "memory": f".claude/agent-memory/{consumer}/", "signals": [],
        }})
        assert inbox_command(case, "role", consumer, "--why", "Fixture evidence") == 0
        finish_session(case, case.launches[0])
        return [observe(case.turns[0])]
    from foragent.orch_spawn import ensure_orchestrator
    from test_orch_spawn import orch_spawn_case
    case, commands, _ = orch_spawn_case(history, root, mp, screen="background")
    write_skills(case.root, names=sorted(installed))
    case.config = configure(case.root, table)
    assert ensure_orchestrator(case.root, case.store, case.config, now=case.clock[0]) == "background"
    argv, = [argv for argv in commands if Path(argv[0]).name == "claude"]
    return [observe({"worktree": case.root, "prompt": "\n".join(argv), "argv": argv})]


@pytest.mark.parametrize("consumer,executor", [
    *[(role, ex) for role in ("workers", "no-lane", "reviewers", "docs", "architect") for ex in ("codex", "claude")],
    ("orchestrator", "claude"),
])
def test_assigned_skill_reaches_only_its_role(git_history, tmp_path, monkeypatch, consumer, executor):
    table = assignments()
    for value in run_consumer(consumer, executor, git_history, tmp_path, monkeypatch, table):
        assert_role_input(value, "workers" if consumer == "no-lane" else consumer, table)


@pytest.mark.parametrize("role", ROLE_NAMES)
@pytest.mark.parametrize("mode", ["empty", "unset"])
def test_empty_or_unset_role_adds_nothing_despite_other_role_assignments(git_history, tmp_path, monkeypatch, role, mode):
    table = assignments()
    if mode == "empty":
        table[role] = []
    else:
        table.pop(role)
    for value in run_consumer(role, "claude" if role == "orchestrator" else "codex", git_history, tmp_path, monkeypatch, table):
        assert_role_input(value, role, table)


@pytest.mark.parametrize("table", [None, {}], ids=["setting-omitted", "all-roles-unset"])
def test_all_unset_keeps_existing_session_behavior(git_history, tmp_path, monkeypatch, table):
    from test_risk_map import copy_history
    for role in ROLE_NAMES:
        root = tmp_path / role
        history = copy_history(git_history, root)
        with monkeypatch.context() as mp:
            seen = run_consumer(role, "claude" if role == "orchestrator" else "codex", history, root, mp, table)
            for value in seen:
                assert_role_input(value, role, table)


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_trusted_catalog_does_not_expose_another_roles_defaults(git_history, tmp_path, monkeypatch, executor):
    from foragent.runner import run_issue
    table = assignments()
    case = configured_case(git_history, tmp_path, monkeypatch, executor, table=table)
    library = tmp_path / "library"
    write_skills(library, names=(*ROLE_SKILLS.values(), OTHER))
    case.config = case.ctx.config = configure(case.root, table, skill_libraries=[{
        "name": "fixture-library", "repo": "https://github.com/anthropics/skills", "path": str(library),
    }])
    seen = []
    case.implementer.steps = [lambda call: (seen.append(received(call, tmp_path)), case.implement(call))[1]]
    assert run_issue(1, case.ctx) == "merged"
    assert_role_input(seen[0], "workers", table)
    assert OTHER in seen[0], "Unassigned trusted skills remain discoverable without roles"


def test_explicitly_shared_skill_can_reach_both_assigned_roles(git_history, tmp_path, monkeypatch):
    from foragent.runner import run_issue
    from test_review_perspective_fanout import is_lane_turn
    table = {"workers": [CHOSEN], "reviewers": [CHOSEN]}
    case = configured_case(git_history, tmp_path, monkeypatch, table=table)
    seen = {"workers": [], "reviewers": []}
    def execute(**call):
        role = "workers" if is_lane_turn(call) else "reviewers"
        seen[role].append(received(call, tmp_path))
        if role == "workers":
            case.implement(call)
            return ExecResult(0, False, "Done", call["log_path"])
        native = call.get("review_level") is not None
        return ExecResult(0, False, json.dumps({"result": "(none)"}) if native else "VERDICT: PASS", call["log_path"])
    case.ctx.executor_factory = lambda name: SimpleNamespace(run=execute)
    assert run_issue(1, case.ctx) == "merged"
    for role, values in seen.items():
        assert values
        for value in values:
            assert_role_input(value, role, table)


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_default_usage_is_counted_from_the_read_log(git_history, tmp_path, monkeypatch, executor):
    from foragent.runner import run_issue
    from test_trusted_skill_use import read_events
    case = configured_case(git_history, tmp_path, monkeypatch, executor, table=assignments())
    def execute(call):
        path = call["worktree"] / "skills" / CHOSEN / "SKILL.md"
        with call["log_path"].open("a") as stream:
            stream.write("\n".join(json.dumps(event) for event in read_events(executor, [path])) + "\n")
        case.implement(call)
        return "Skills used: none"
    case.implementer.steps = [execute]
    assert run_issue(1, case.ctx) == "merged"
    assert case.store.get_run(1).skills_used == [CHOSEN]
    assert f"Skills used: {CHOSEN}" in case.tracker.created_prs[0].body.splitlines()


def test_native_review_transport_gets_only_reviewer_defaults(git_history, tmp_path, monkeypatch):
    from foragent import executors
    from foragent.runner import run_issue
    table = assignments()
    case = configured_case(git_history, tmp_path, monkeypatch, table=table)
    seen = []
    def transport(argv, **kw):
        text = "\n".join(argv) + "\n" + (kw.get("prompt") or "")
        scope = kw["cwd"] / "CLAUDE.local.md"
        if scope.exists(): text += scope.read_text()
        seen.append(received({"worktree": kw["cwd"], "prompt": text}, tmp_path))
        native = any(arg.startswith("/code-review") for arg in argv)
        kw["stdout_path"].write_text(json.dumps({"result": "(none)"} if native else {"type": "result", "result": "VERDICT: PASS"}))
        return 0, False, ""
    monkeypatch.setattr(executors, "run_logged", transport)
    case.ctx.executor_factory = lambda name: case.implementer if name == "codex" else executors.ClaudeExecutor()
    assert run_issue(1, case.ctx) == "merged"
    assert seen
    for value in seen: assert_role_input(value, "reviewers", table)


@pytest.mark.parametrize("license,allowed", [("MIT", True), ("LicenseRef-Fixture-Restricted", False), ("unknown", False)])
def test_init_installs_the_role_table_using_license_facts(setup, isolated_home, monkeypatch, capsys, license, allowed):
    from foragent import cli
    from foragent.git import GitRepo
    library = setup.root.parent / "library"
    write_skills(library)
    restricted = ROLE_SKILLS["reviewers"]
    (library / "skills" / restricted / "LICENSE").write_text(f"SPDX-License-Identifier: {license}\n")
    write_global_config({**setting_values(assignments()), "skill_libraries": [{
        "name": "fixture-library", "repo": "https://github.com/anthropics/skills", "path": str(library),
    }]})
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "init_repo", lambda root, **kw: setup.init(**kw))
    code = cli.main(["init", "--repo-root", str(setup.root), "--approver", "human", "--no-push", "--no-launchd", "--no-board", "--statusline", "no"])
    if allowed:
        assert code == 0
        for name in ROLE_SKILLS.values():
            assert marker(name) in (setup.root / ".claude/skills" / name / "SKILL.md").read_text()
    else:
        assert code != 0 and not (setup.root / ".claude/skills" / restricted).exists()
        output = capsys.readouterr()
        assert restricted in output.err and "license" in output.err.lower()


def test_public_configuration_rejects_a_broadcast_list_and_bad_role_values(tmp_path):
    from foragent.config import ConfigError
    root = tmp_path / "project";root.mkdir()
    configure(root, assignments())
    for invalid in ([CHOSEN], {"workers": CHOSEN}, {"workers": [3]}):
        (root / ".foragent.yml").write_text(yaml.safe_dump({"skill_libraries": [], "default_skills": invalid}))
        with pytest.raises(ConfigError, match="default_skills"):
            load_config(root)
