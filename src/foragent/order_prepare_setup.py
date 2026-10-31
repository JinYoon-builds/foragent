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

from foragent.order_scratch_context import Context
from foragent.config import Config
from foragent.lanes import agent_text
from foragent.skills import SkillManager, with_vendored_library


EXECUTOR_SETTINGS = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json", ".codex/config.toml")


def _proposed(plan: dict, lane: str, config: Config, root: Path, context: Context) -> dict | None:
    """A lane this order creates: the settings add_lane would write; its files go into ``context``.

    Each library skill folder add_lane would install is a whole-tree source at
    ``.claude/skills/<name>``; the agent definition is generated as add_lane writes it.
    """
    proposal = next((entry for entry in plan.get("new_lanes", []) if entry["name"] == lane), None)
    if proposal is None or lane in config.lanes:
        return None
    manager = SkillManager(with_vendored_library(config, Path.cwd(), root)) if proposal["skills"] else None
    installed = []
    for identity in proposal["skills"]:
        skill = manager.check_install(identity, root)  # the same resolution add_lane installs from
        installed.append(skill.path.name)
        context.sources.append((skill.path, f".claude/skills/{skill.path.name}"))
    context.files[f".claude/agents/fa-{lane}.md"] = agent_text(lane, list(proposal["paths"]), installed).encode("utf-8")
    return {"paths": list(proposal["paths"]), "agent": f"fa-{lane}", "skills": installed}


def _checkout_context(config: Config, lane: str, root: Path, context: Context) -> None:
    """The checkout's context folders for the lane, each copied whole into the scratch.

    The agents folder, the lane's memory folder (found by the resolver the implementer's notes
    use, also for a lane without an agent) and each configured lane skill folder.
    """
    from foragent.run_lane_skills import _agent_text, _memory_folder, lane_skill_names

    context.add_checkout(".claude/agents")
    memory = _memory_folder(config, lane, Path("."))
    if memory is not None:
        context.add_checkout(memory.as_posix())
    for name in lane_skill_names(config, lane, root, _agent_text(config, lane, root)[0]):
        if re.fullmatch(r"[A-Za-z0-9._-]+", name):
            context.add_checkout(f".claude/skills/{name}")


def lane_context(plan: dict, item: dict, config: Config, root: Path) -> tuple[Config, Context]:
    """The configuration with the issue's lane resolved (proposed lanes included) and its context.

    ``Context.apply`` copies the sources into a scratch tree; its ``refused`` paths then hold
    every context entry that could not be reproduced faithfully. A proposed lane's library
    skills come first, so they win over a checkout folder of the same name.
    """
    context = Context(root)
    settings = _proposed(plan, item["lane"], config, root, context)
    if settings is not None:
        config = replace(config, lanes={**config.lanes, item["lane"]: settings})
    _checkout_context(config, item["lane"], root, context)
    # Settings committed on main could run commands outside the writer's sandbox (hooks, status
    # line, credential helpers, plugins, MCP servers): the scratch keeps none of them.
    context.sources += [(None, name) for name in EXECUTOR_SETTINGS]
    return config, context


def writer_setup(config: Config, item: dict, tree: Path):
    """The implementer setup preread uses for this lane and size, read from the scratch tree."""
    from foragent.run_implementation import implementer_setup, lane_config

    return implementer_setup(lane_config(config, item["lane"]), item["lane"], item["size"], tree)


def _rule(tool: str, path: Path) -> str:
    return f"{tool}(//{path.as_posix().lstrip('/')})"


def permissions(executor: str, tree: Path, protected: tuple[Path, ...], acceptance_dir: str,
                directory: Path, *, edit: bool = True) -> dict:
    """Executor options that confine the writer to the tree, ignoring any inherited authority.

    Codex runs without the user's config, execpolicy rules, hooks, notify program and MCP
    servers, so nothing widens its workspace-write sandbox or runs outside it. Claude loads no
    user or local settings, the scratch holds no project settings (``EXECUTOR_SETTINGS``), no
    MCP server is configured and all hooks are disabled; it may Edit only the tree's acceptance
    directory and its sandboxed shell may write only in the tree, never in a ``protected``
    directory (checkout, Git directory, state) or the tree's own Claude settings, with no network.
    Without ``edit`` (the reviewer) Claude has no edit tools at all; its shell keeps the same sandbox.
    """
    if executor != "claude":
        return {"isolated": True}
    tests = tree / acceptance_dir.strip("/")
    # The state directory holds the tree itself; the sandbox already keeps shell writes in the tree.
    denied = [place for place in protected if not tree.resolve().is_relative_to(place)] + [tree / ".claude"]
    settings = {
        "permissions": {
            "allow": ["Read", "Glob", "Grep", "Bash"] + (
                [_rule("Edit", tests / "**"), _rule("Write", tests / "**")] if edit else []),
            "deny": ["WebFetch", "WebSearch", *(_rule(tool, place / "**") for place in denied
                                                for tool in ("Edit", "Write"))]
            + ([] if edit else ["Edit", "Write", "NotebookEdit"]),
        },
        "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
                    "filesystem": {"allowWrite": [str(tree)], "denyWrite": [str(place) for place in denied]},
                    "network": {"allowedDomains": []}},
        "enableAllProjectMcpServers": False,
        # Hooks run with the user's full permissions outside the sandbox, whoever defines them
        # (settings, agent or skill frontmatter, plugins): none run for the writer.
        "disableAllHooks": True,
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude-settings.json"
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"settings_path": path, "permission_mode": "dontAsk", "setting_sources": "project"}
