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


def _proposed(plan: dict, lane: str, config: Config, root: Path) -> tuple[dict | None, dict[str, bytes | None]]:
    """A lane this order creates: the settings and files add_lane would install for it."""
    proposal = next((entry for entry in plan.get("new_lanes", []) if entry["name"] == lane), None)
    if proposal is None or lane in config.lanes:
        return None, {}
    manager = SkillManager(with_vendored_library(config, Path.cwd(), root)) if proposal["skills"] else None
    files, installed = {f".claude/agents/fa-{lane}.md": None}, []
    for identity in proposal["skills"]:
        skill = manager.check_install(identity, root)  # the same resolution add_lane installs from
        installed.append(skill.path.name)
        files[f".claude/skills/{skill.path.name}"] = None  # main's older copy must not linger
        for path in sorted(skill.path.rglob("*")):
            if path.is_file():
                files[f".claude/skills/{skill.path.name}/{path.relative_to(skill.path).as_posix()}"] = path.read_bytes()
    files[f".claude/agents/fa-{lane}.md"] = agent_text(lane, list(proposal["paths"]), installed).encode("utf-8")
    return {"paths": list(proposal["paths"]), "agent": f"fa-{lane}", "skills": installed}, files


def _checkout_context(config: Config, lane: str, root: Path) -> dict[str, bytes | None]:
    """The lane's agent, memory and skills as the checkout has them, which may not be on main yet.

    The paths come from the resolvers the implementer's own notes use (run_lane_skills), so the
    writer sees the same files: the agent file when there is one, the lane's memory directory
    even without an agent, and the configured lane skills. Each place maps to ``None`` first so
    main's copy is removed: what the checkout deleted is gone from the scratch tree as well.
    """
    from foragent.run_lane_skills import _agent_text, _lane_settings, _memory_folder, lane_skill_names

    named = _lane_settings(config, lane).get("agent")
    agent = named if isinstance(named, str) and re.fullmatch(r"fa-[a-z0-9-]+", named) else None
    places = [f".claude/agents/{agent}.md"] if agent is not None else []
    memory = _memory_folder(config, lane, Path("."))
    if memory is not None:
        places.append(memory.as_posix())
    places += [f".claude/skills/{name}" for name in lane_skill_names(config, lane, root, _agent_text(config, lane, root)[0])
               if re.fullmatch(r"[A-Za-z0-9._-]+", name)]
    files: dict[str, bytes | None] = {}
    for place in places:
        files[place] = None
        start = root / place
        paths = [start] if start.is_file() else sorted(start.rglob("*")) if start.is_dir() else []
        for path in paths:
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def lane_context(plan: dict, item: dict, config: Config, root: Path) -> tuple[Config, dict[str, bytes | None]]:
    """The configuration with the issue's lane resolved (proposed lanes included) and its files.

    The files are a scratch overlay: ``None`` removes a place, bytes write a file.
    """
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


def permissions(executor: str, tree: Path, protected: tuple[Path, ...], acceptance_dir: str,
                directory: Path) -> dict:
    """Executor options that confine the writer to the tree, ignoring any inherited authority.

    Codex runs without the user's config and execpolicy rules, so no configured writable
    root or network access widens its workspace-write sandbox. Claude loads only the
    tree's project settings plus this file, no user or local settings and no MCP server;
    it may Edit only the tree's acceptance directory and its sandboxed shell may write only
    in the tree, never in a ``protected`` directory (checkout, Git directory, state) or the
    tree's own Claude settings, with no network.
    """
    if executor != "claude":
        return {"isolated": True}
    tests = tree / acceptance_dir.strip("/")
    # The state directory holds the tree itself; the sandbox already keeps shell writes in the tree.
    denied = [place for place in protected if not tree.resolve().is_relative_to(place)] + [tree / ".claude"]
    settings = {
        "permissions": {
            "allow": ["Read", "Glob", "Grep", "Bash", _rule("Edit", tests / "**"), _rule("Write", tests / "**")],
            "deny": ["WebFetch", "WebSearch", *(_rule(tool, place / "**") for place in denied
                                                for tool in ("Edit", "Write"))],
        },
        "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                    "filesystem": {"allowWrite": [str(tree)], "denyWrite": [str(place) for place in denied]},
                    "network": {"allowedDomains": []}},
        "enableAllProjectMcpServers": False,
    }
    path = directory / "claude-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"settings_path": path, "permission_mode": "dontAsk", "setting_sources": "project"}
