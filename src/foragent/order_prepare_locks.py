"""Edits of an issue already published with behaviour statements, and its locked tests (#2558).

The published issue's acceptance files are on main. An edit of it is prepared only for what it
changes:

- a statement unchanged from the published body is already covered by the issue's existing locks,
  so neither the writer nor the reviewer has to cover it again (``covered``);
- an edit that changes nothing but existing locked files (goal, scope, statements and Done when
  unchanged) needs no preparation at all (``lock_only``); the publication recheck proves it;
- an edit that changes a statement may carry a corrected version of an existing lock in the plan:
  that file is placed in the writer's tree as a candidate, so the writer may keep or revise it and
  the reviewer reviews it with the attempt (``carried``).

``facts`` adds what the published body says to the edit's view; preparation and publication read
the edit through the same view, so both agree on what counts.
"""

from __future__ import annotations

from foragent.issue import _sections, acceptance_paths, has_behaviour, parse_body


# The only fields a lock-only fix may state; each must equal the published body. Any other field
# (blocked_by, references, decisions, done_when_removals...) is a change this exemption cannot prove.
_RESTATED = {"number", "goal", "out_of_scope", "statements", "done_when", "checklist", "files", "exclusive",
             "acceptance_files"}


def published(body: str, acceptance_dir: str) -> dict:
    """What a published body states: statements, goal, scope, files, exclusive paths, Done when,
    checklist, whether it lists references, and its locked acceptance files."""
    parsed = parse_body(body)
    sections = _sections(body.replace("\r\n", "\n"))
    lines = sections.get("Behaviour", [])
    statements = [line.strip()[2:].strip() for line in lines if line.strip().startswith(("- ", "* "))]
    return {"statements": statements, "goal": parsed.goal, "out_of_scope": parsed.out_of_scope,
            "done_when": parsed.done_when_commands, "files": parsed.files, "exclusive": parsed.exclusive,
            "checklist": [entry.text for entry in parsed.done_when_checklist],
            "references": "## References" in body.replace("\r\n", "\n").split("\n"),
            "locks": acceptance_paths(parsed.done_when_commands, acceptance_dir)}


def _restates(item: dict, before: dict) -> bool:
    """Whether the edit states exactly what the published body states, its acceptance files aside.

    Fail closed: an unknown field, a field the body cannot show unchanged, or any difference means
    the edit changes the issue and needs preparation."""
    return (set(item) <= _RESTATED and not before["references"]
            and item["goal"].strip() == before["goal"] and item["out_of_scope"].strip() == before["out_of_scope"]
            and item["statements"] == before["statements"] and item["done_when"] == before["done_when"]
            and list(item.get("checklist", [])) == before["checklist"]
            and list(item.get("files", [])) == before["files"]
            and list(item.get("exclusive", [])) == before["exclusive"])


def facts(item: dict, body: str, acceptance_dir: str) -> dict:
    """The keys an edit's view carries about its published issue; none for other edits.

    ``locks``: the published Done when's acceptance files. ``covered``: statements unchanged from
    the body, counted only while the edit still runs every published lock command and carries no
    correction of a lock (a corrected lock no longer proves what the old one did). ``lock_only``:
    the edit restates the issue exactly and only replaces some of its existing locks.
    """
    if "statements" not in item or not has_behaviour(body or ""):
        return {}
    before = published(body, acceptance_dir)
    if not before["locks"]:
        return {}
    view = {"locks": before["locks"]}
    files = set(item.get("acceptance_files") or {})
    if _restates(item, before) and files and files <= set(before["locks"]):
        view["lock_only"] = True
        return view
    lock_commands = [command for command in before["done_when"]
                     if set(acceptance_paths([command], acceptance_dir)) & set(before["locks"])]
    if not files & set(before["locks"]) and all(command in item["done_when"] for command in lock_commands):
        covered = [line for line in item["statements"] if line in before["statements"]]
        if covered:
            view["covered"] = covered
    return view


def covered(item: dict) -> list[str]:
    """Statements the issue's existing locked tests already cover."""
    return list(item.get("covered") or [])


def lock_only(item: dict) -> bool:
    return bool(item.get("lock_only"))


def carried(item: dict) -> dict[str, str]:
    """Corrected versions of the issue's own existing locks that the plan carries for this attempt."""
    locks = set(item.get("locks") or [])
    return {name: content for name, content in (item.get("acceptance_files") or {}).items()
            if name in locks and isinstance(content, str)}


def kept_commands(item: dict, commands: list[str], acceptance_dir: str) -> list[str]:
    """The edit's Done when lines that run its existing locks stay beside the writer's commands."""
    locks = set(item.get("locks") or [])
    kept = [command for command in item.get("done_when", [])
            if command not in commands and set(acceptance_paths([command], acceptance_dir)) & locks]
    return kept + list(commands) if kept and commands else list(commands)


def prompt_section(item: dict) -> str:
    """What the writer and reviewer read about the published issue's existing locks."""
    text = ""
    if covered(item):
        text += ("\n## Statements already covered by this issue's locked tests\nThe issue was published "
                 "with these statements; its existing locked tests on main cover them. Do not write or map\n"
                 "tests for them; handle only the other statements:\n"
                 + "".join(f"- {line}\n" for line in covered(item)))
    if carried(item):
        text += ("\n## Corrected locked tests carried by this edit\nThese existing locked files of this issue "
                 "are part of the attempt: the corrected version is in the tree and is reviewed with it:\n"
                 + "".join(f"- {name}\n" for name in sorted(carried(item))))
    return text
