"""Edits of an issue already published with behaviour statements, and its locked tests (#2558).

The published issue's acceptance files are on main. An edit of it is prepared only for what it
changes:

- a statement unchanged from the published body is already covered by the issue's existing locks,
  so neither the writer nor the reviewer has to cover it again (``covered``);
- an edit that changes nothing but existing locked files (the complete authored body and state
  unchanged) needs no preparation at all (``lock_only``); the publication recheck proves it;
- an edit that changes a statement may carry a corrected version of an existing lock in the plan:
  that file is placed in the writer's tree as a candidate, so the writer may keep or revise it and
  the reviewer reviews it with the attempt (``carried``).

``facts`` adds what the published body says to the edit's view; preparation and publication read
the edit through the same view, so both agree on what counts.
"""

from __future__ import annotations

from foragent.issue import MARKER, _sections, acceptance_paths, has_behaviour, parse_body
from foragent.order_files import acceptance_links, issue_body


# These fields only render the body or identify the edit. Compare all authored body text;
# fields outside it (including label changes and dependencies) cannot use this exemption.
_RESTATED = {"number", "goal", "out_of_scope", "statements", "done_when", "checklist", "files", "exclusive",
             "acceptance_files"}


def published(body: str, acceptance_dir: str) -> dict:
    """Published statements, Done when commands and locked acceptance files, for coverage."""
    parsed = parse_body(body)
    sections = _sections(body.replace("\r\n", "\n"))
    lines = sections.get("Behaviour", [])
    statements = [line.strip()[2:].strip() for line in lines if line.strip().startswith(("- ", "* "))]
    return {"statements": statements,
            "done_when": parsed.done_when_commands,
            "locks": acceptance_paths(parsed.done_when_commands, acceptance_dir)}


def _restates(item: dict, body: str, repo: str | None, base: str, acceptance_dir: str) -> bool:
    """Whether the edit states exactly what the published body states, its acceptance files aside.

    Fail closed: an unknown field, a field the body cannot show unchanged, or any authored difference means
    the edit changes the issue and needs preparation."""
    if not set(item) <= _RESTATED or repo is None:
        return False
    links = [line + "\n" for line in acceptance_links(item, repo, base, acceptance_dir)]
    authored = issue_body(item, repo, base, acceptance_dir).removeprefix(MARKER + "\n")
    authored = authored.removesuffix("".join(links))
    current = body.removeprefix(MARKER + "\n")
    if not current.startswith(authored):
        return False
    # Older issues may lack the renderer's version marker or some acceptance links. Only
    # these exact generated footer lines may follow the authored text, once and in order.
    # A link inside a goal, scope, checklist or any other authored section stays in the comparison.
    footer = current[len(authored):]
    for link in links:
        footer = footer.removeprefix(link)
    return not footer


def facts(item: dict, body: str, acceptance_dir: str, *, repo: str | None, base: str,
          state: str = "open") -> dict:
    """The keys an edit's view carries about its published issue; none for other edits.

    ``locks``: the published Done when's acceptance files. ``covered``: statements unchanged from
    the body, counted only while the edit still runs every published lock command and carries no
    correction of a lock (a corrected lock no longer proves what the old one did). ``lock_only``:
    the edit restates the issue exactly and only replaces some of its existing locks.
    """
    before = published(body, acceptance_dir)
    if not before["locks"]:
        return {}
    view = {}
    files = set(item.get("acceptance_files") or {})
    if (state == "open" and _restates(item, body, repo, base, acceptance_dir)
            and files and files <= set(before["locks"])):
        view["lock_only"] = True
    if "statements" not in item or not has_behaviour(body or ""):
        return view
    view["locks"] = before["locks"]
    if view.get("lock_only"):
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


def covered_only(item: dict) -> bool:
    """Every statement of the edit is covered by the issue's existing locks and nothing new is carried.

    Such an edit gives a test writer nothing to do (PM q82): preparation records it reviewed with its
    existing Done when instead of starting a writer or reviewer; publication still rechecks it."""
    statements = list(item.get("statements") or [])
    return bool(statements) and set(statements) <= set(covered(item)) and not item.get("acceptance_files")


def settle_covered(plan: dict, item: dict, record: dict, *, cycle: str, asked: str, base: str,
                   behaviour: str, published: dict, others: dict, reason: str, now: str) -> None:
    """Record a covered-only edit as reviewed on ``base`` for ``cycle``; no agent starts."""
    entry = record["issues"].setdefault(item["key"], {"key": item["key"], "attempts": []})
    for stale in ("coverage", "red", "carried", "candidate_input", "locks", "scratch"):
        entry.pop(stale, None)
    entry.update(status="reviewed", phase="reviewing_tests", questions=[], notes=[], reason=reason, cycle=cycle,
                 requirement=asked, behaviour=behaviour, published=published, base_revision=base,
                 others=others, written={}, done_when=list(item.get("done_when") or []), ignored=[],
                 updated_at=now)
