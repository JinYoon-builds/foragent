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

from foragent import decisions as decision_links, order_done_when, order_waiting
from foragent.issue import MARKER, _sections, acceptance_paths, has_behaviour, parse_body
from foragent.order_files import acceptance_links, issue_body
from foragent.ownership import OWNED_MARKER

_NOTES = "## Preparation notes"


def notes_section(notes: list[str]) -> str:
    """The Preparation notes section publish writes after a prepared issue's links (#2868, r1293).

    Its one writer is ``order._published_body``. Each note is one ``- `` line: its own line breaks and
    runs of whitespace fold into single spaces, so no note line can be mistaken for a person's text and
    ``_generated`` reads the section back in exactly this form."""
    return f"\n{_NOTES}\n" + "".join(f"- {' '.join(str(note).split())}\n" for note in notes) if notes else ""


def _child_line():
    from foragent.order import _CHILD_LINE  # order imports this module

    return _CHILD_LINE


# The generated tail of a published issue body, described once (#2868). After the acceptance links
# publication writes ``notes_section``; after it, publication and the gate add only these whole lines,
# each in its writer's own form: plan-child marker (order), owner label (ownership), decisions
# (decisions.with_marker), waiting-prior and write ids (order_waiting.stamp and mark_write). The Done
# when removal record (order_done_when) and the gate's ``## Waiting`` section are the two generated
# blocks; ``_generated`` takes each out only in its writer's exact rendering.
GENERATED_LINES = (lambda line: line == OWNED_MARKER, lambda line: bool(_child_line().fullmatch(line)),
                   decision_links.MARKER.fullmatch, order_waiting._PRIOR.fullmatch, order_waiting._WRITE.fullmatch)


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
    the edit changes the issue and needs preparation. ``fa order edit`` seeds the gate's wait into the goal
    (``order_done_when.seed``), so publishing the edit keeps it; only for that seeded goal is the gate's
    ``## Waiting`` section in the tail generated rather than authored."""
    if not set(item) <= _RESTATED or repo is None:
        return False
    waiting = order_waiting.section(body)
    suffix = None if waiting is None else f"\n\n## Waiting\n{waiting}"
    seeded = suffix is not None and str(item.get("goal", "")).endswith(suffix)
    return (_restated(item, body, repo, base, acceptance_dir, None)
            or seeded and _restated({**item, "goal": item["goal"].removesuffix(suffix)}, body, repo, base,
                                    acceptance_dir, waiting))


def _restated(item: dict, body: str, repo: str, base: str, acceptance_dir: str, waiting: str | None) -> bool:
    links = [line + "\n" for line in acceptance_links(item, repo, base, acceptance_dir)]
    authored = issue_body(item, repo, base, acceptance_dir).removeprefix(MARKER + "\n")
    authored = authored.removesuffix("".join(links))
    current = body.removeprefix(MARKER + "\n")
    if not current.startswith(authored):
        return False
    # Older issues may lack the renderer's version marker or some acceptance links. Only
    # these exact generated footer lines, then the generated tail (``_generated``), may follow
    # the authored text, once and in order.
    # A link inside a goal, scope, checklist or any other authored section stays in the comparison.
    footer = current[len(authored):]
    for link in links:
        footer = footer.removeprefix(link)
    return _generated(footer, repo, waiting)


def _generated(footer: str, repo: str | None = None, waiting: str | None = None) -> bool:
    """Whether ``footer`` (the body after the acceptance links) is exactly the generated tail.

    That is ``notes_section`` right after the links, then only ``GENERATED_LINES``, blank lines, the
    removal record and (``waiting`` given) the gate's Waiting section, each in its writer's exact form.
    Any other line, wherever a person typed it, is authored: the edit needs preparation and publication
    never writes over it. Where this machine still holds the preparation record that the plan-child
    marker names, the notes must also be exactly the ones it renders."""
    text = footer.replace("\r\n", "\n")
    head = f"\n{_NOTES}\n"
    notes = None
    if text.startswith(head):
        lines = text[len(head):].split("\n")
        count = next((index for index, line in enumerate(lines) if not line.startswith("- ")), len(lines))
        if not count:
            return False
        notes = lines[:count]
        text = "\n" + "\n".join(lines[count:])
    if waiting is not None:
        block = "\n" + order_waiting.replace_section("", waiting).rstrip("\n") + "\n"
        if text.count(block) != 1:
            return False
        text = text.replace(block, "\n", 1)
    span = order_done_when._span(text)
    if span is not None:
        if text[span[0]:span[1]].strip() != order_done_when._render(order_done_when.records(text)).strip():
            return False
        text = text[:span[0]] + text[span[1]:]
    if not all(not line or any(form(line) for form in GENERATED_LINES) for line in text.split("\n")):
        return False
    recorded = _recorded_notes(text, repo) if notes is not None else None
    return recorded is None or "\n".join(notes) + "\n" == notes_section(recorded).removeprefix(head)


def _recorded_notes(markers: str, repo: str | None) -> list[str] | None:
    """The notes publish wrote, from the preparation record the plan-child marker names; None if absent."""
    from foragent.order_reviewed import _record

    child = next((match for line in markers.split("\n") if (match := _child_line().fullmatch(line))), None)
    if child is None:
        return None
    notes = _record({"id": child[1]}, repo).get(child[2], {}).get("notes")
    return notes if isinstance(notes, list) and notes and all(isinstance(n, str) for n in notes) else None


def facts(item: dict, body: str, acceptance_dir: str, *, repo: str | None, base: str,
          state: str = "open", existing=None) -> dict:
    """The keys an edit's view carries about its published issue; none for other edits.

    ``locks``: the published Done when's acceptance files. ``covered``: statements unchanged from
    the body, counted only while the edit still runs every published lock command and carries no
    correction of a lock (a corrected lock no longer proves what the old one did). ``lock_only``:
    the edit restates the issue exactly and only replaces some of its existing locks.
    ``beside``: replaced locked files outside its own Done when; they count only when ``existing``
    (a name -> bool check against the base) finds every one, so a new file still needs preparation.
    """
    before = published(body, acceptance_dir)
    if not before["locks"]:
        return {}
    view = {}
    files = set(item.get("acceptance_files") or {})
    outside = files - set(before["locks"])
    if (state == "open" and _restates(item, body, repo, base, acceptance_dir) and files
            and (not outside or existing is not None
                 and all(_under(name, acceptance_dir) and existing(name) for name in outside))):
        view["lock_only"] = True
        # Locked acceptance files outside this issue's Done when (a helper its tests import, a
        # closed issue's lock): the publication check proves them and decides where they land.
        if outside:
            view["beside"] = sorted(outside)
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


def _under(name: str, acceptance_dir: str) -> bool:
    folder = acceptance_dir.rstrip("/") + "/"
    return name.startswith(folder) and ".." not in name.split("/")


def beside(item: dict) -> list[str]:
    """Corrected locked files outside the issue's own Done when that a lock-only edit carries."""
    return list(item.get("beside") or [])


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
