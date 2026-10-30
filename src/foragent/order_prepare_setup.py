"""How `fa order prepare` starts a lane's test writer (#1047).

The lane's context (agent, memory, skills) is put into the scratch tree first, also for a
lane this order only proposes, and the implementer setup is then read from that tree, so
the writer gets exactly what the tree holds and a missing skill is a fact, not a guess.
Write permission is structural: Codex's workspace-write sandbox has the tree as its only
writable root and no network; Claude refuses every tool its settings do not allow and
runs shell commands in its sandbox, whose writes stay in the tree.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re

from foragent.config import Config
from foragent.lanes import agent_text
from foragent.skills import SkillManager, with_vendored_library


def _proposed(plan: dict, lane: str, config: Config, root: Path) -> tuple[dict | None, dict[str, bytes]]:
    """A lane this order creates: the settings and files add_lane would install for it."""
    proposal = next((entry for entry in plan.get("new_lanes", []) if entry["name"] == lane), None)
    if proposal is None or lane in config.lanes:
        return None, {}
    manager = SkillManager(with_vendored_library(config, Path.cwd(), root)) if proposal["skills"] else None
    files, installed = {}, []
    for identity in proposal["skills"]:
        skill = manager.check_install(identity, root)  # the same resolution add_lane installs from
        installed.append(skill.path.name)
        for path in sorted(skill.path.rglob("*")):
            if path.is_file():
                files[f".claude/skills/{skill.path.name}/{path.relative_to(skill.path).as_posix()}"] = path.read_bytes()
    files[f".claude/agents/fa-{lane}.md"] = agent_text(lane, list(proposal["paths"]), installed).encode("utf-8")
    return {"paths": list(proposal["paths"]), "agent": f"fa-{lane}", "skills": installed}, files


def _checkout_context(config: Config, lane: str, root: Path) -> dict[str, bytes]:
    """An existing lane's agent, memory and skills from the checkout, which may not be on main yet."""
    settings = config.lanes.get(lane, {}) if lane != "main" else {}
    agent = settings.get("agent")
    places = []
    if isinstance(agent, str) and re.fullmatch(r"fa-[a-z0-9-]+", agent):
        places += [f".claude/agents/{agent}.md", f".claude/agent-memory/{agent}"]
    places += [f".claude/skills/{name}" for name in settings.get("skills", []) or []
               if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9._-]+", name)]
    files = {}
    for place in places:
        start = root / place
        paths = [start] if start.is_file() else sorted(start.rglob("*")) if start.is_dir() else []
        for path in paths:
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def lane_context(plan: dict, item: dict, config: Config, root: Path) -> tuple[Config, dict[str, bytes]]:
    """The configuration with the issue's lane resolved (proposed lanes included) and its files."""
    settings, proposed = _proposed(plan, item["lane"], config, root)
    if settings is not None:
        config = replace(config, lanes={**config.lanes, item["lane"]: settings})
    return config, _checkout_context(config, item["lane"], root) | proposed


def writer_setup(config: Config, item: dict, tree: Path):
    """The implementer setup preread uses for this lane and size, read from the scratch tree."""
    from foragent.run_implementation import implementer_setup, lane_config

    return implementer_setup(lane_config(config, item["lane"]), item["lane"], item["size"], tree)


def _rule(tool: str, path: Path) -> str:
    return f"{tool}(//{path.as_posix().lstrip('/')})"


def permissions(executor: str, tree: Path, root: Path, acceptance_dir: str, directory: Path) -> dict:
    """Executor options that confine the writer to the tree (Claude only; see the module note)."""
    if executor != "claude":
        return {}
    tests = tree / acceptance_dir.strip("/")
    settings = {
        "permissions": {
            "allow": ["Read", "Glob", "Grep", "Bash", _rule("Edit", tests / "**"), _rule("Write", tests / "**")],
            "deny": ["WebFetch", "WebSearch", _rule("Edit", root / "**"), _rule("Write", root / "**")],
        },
        "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False},
        "enableAllProjectMcpServers": False,
    }
    path = directory / "claude-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"settings_path": path, "permission_mode": "dontAsk"}
