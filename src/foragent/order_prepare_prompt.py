"""The prompt `fa order prepare` gives a lane's test writer (#1047)."""

from __future__ import annotations

import json
from pathlib import Path

from foragent import order_references, risks
from foragent.config import Config

REPLY_SHAPE = '{"acceptance_files": {"<path>": "<full file text>"}, "done_when": ["<command>"], "questions": []}'


def _references(plan: dict, item: dict) -> dict[str, bytes]:
    contents = order_references._decoded(plan, order_references.selected(plan, item))
    return {name: value.encode("utf-8") if isinstance(value, str) else value for name, value in contents.items()}


def _risk_notes(root: Path, config: Config, item: dict) -> str:
    paths = list(item.get("files", [])) + list(config.lanes.get(item["lane"], {}).get("paths", []))
    try:
        return risks.review_notes(risks.matching(risks.load(root), paths), paths)
    except risks.RiskError as exc:
        return f"Risk map {risks.RISK_FILE} could not be read: {exc}"


def writer_prompt(plan: dict, item: dict, config: Config, root: Path, tree: Path, base: str) -> str:
    """What the lane's test writer receives: statements, scope, contracts and the rules of the tree."""
    directory = config.acceptance_dir
    titles = {issue["key"]: issue for issue in plan["issues"]}
    blocked = "\n".join(
        f"- {key}: {titles[key]['title']}: {titles[key]['goal']}" if key in titles else f"- {key}"
        for key in item.get("blocked_by", [])) or "(none)"

    statements = "\n".join(f"- {line}" for line in item["statements"])
    files = "\n".join(f"- {path}" for path in item.get("files", [])) or "(not listed)"
    checklist = "\n".join(f"- {line}" for line in item.get("checklist", [])) or "(none)"
    references = order_references.preread_section(plan, item, root, tree) if _references(plan, item) else ""
    notes = _risk_notes(root, config, item) or "(no risk map entry matches this issue)"
    heading = (f"# Edit of existing issue {item['key']}: {item['title']}" if item.get("edit")
               else f"# Issue {item['key']}: {item['title']}")
    return (
        "You are the lane that will implement the issue below in a later, separate run. Now, before\n"
        "publication, write only its acceptance tests. Do not implement the behaviour.\n\n"
        f"Your working directory is a scratch copy of current {config.base_branch} ({base}).\n"
        f"- Create new test files and test helpers only under {directory}. Production files, existing\n"
        "  tests and shared test configuration (conftest.py, project and tool configuration) are read-only:\n"
        "  a change there is refused by path and becomes a question, never part of the tests.\n"
        f"- Do not write to the original checkout ({root}), approval state, credentials or remote services.\n"
        "  Run the tests only inside this scratch tree; outputs that need writes go here.\n"
        "- Every statement needs at least one test that runs through the project's public entry points\n"
        "  and fails on current main by an assertion about the missing behaviour (not an import, syntax\n"
        "  or collection error, and not zero collected tests). Check each test by the break-it rule:\n"
        "  a wrong value, a skipped effect and a doubled effect must each make it fail.\n"
        "- Choose the test tooling from the repository itself.\n"
        "- Process-control behaviour is tested with fake process tables only; never send real signals.\n"
        "- When a statement cannot become a test (unclear, or no observable result), do not weaken or\n"
        "  skip it: add a question naming the missing observable behaviour. When a behaviour cannot be\n"
        "  checked by a machine, ask for it to become a checklist item and still give one runnable check.\n\n"
        f"Reply with one JSON object only: {REPLY_SHAPE}\n"
        "acceptance_files holds the exact text of every file you wrote; done_when the commands that run them.\n\n"
        f"# Order: {plan['title']}\n{plan.get('summary', '')}\n\n"
        f"## Spec\n{plan.get('spec_markdown', '')}\n\n"
        f"New lanes in this order (not created yet): "
        f"{json.dumps(plan.get('new_lanes', []), ensure_ascii=False, sort_keys=True)}\n\n"
        f"{heading}\nLane: {item['lane']}\nSize: {item['size']}\n\n"
        f"## Behaviour statements\n{statements}\n\n"
        f"## Goal\n{item['goal']}\n\n## Out of scope\n{item['out_of_scope']}\n\n"
        f"## Blocked by\n{blocked}\n\n## Files expected to change later\n{files}\n\n"
        f"## Checklist\n{checklist}\n\n"
        f"## Other issues of this order (contracts you may rely on)\nTests already under {directory} in this tree\n"
        "and the locked tests below are read-only: read them, do not change them. Another issue's\n"
        f"statements and goal are its public contract.\n\n{other_issues(plan, item)}\n\n"
        f"## Risk guidance\n{notes}\n"
    ) + (f"\n{references}" if references else "")


def _lines(values) -> str:
    return "".join(f"  - {value}\n" for value in values or []) or "  (none)\n"


def other_issues(plan: dict, item: dict) -> str:
    """Every other issue and edit of the order as the writer and reviewer read them: the same
    ``contracts`` the cycle binds, so a contract declared only in another issue's words reaches both."""
    from foragent.order_prepare_cycle import contracts

    blocks = []
    for key, other in contracts(plan, item).items():
        tests = "".join(f"\n#### {path} (locked)\n```\n{content}\n```\n"
                        for path, content in (other["acceptance_files"] or {}).items())
        commands = "" if other["done_when"] is None else f"Done when:\n{_lines(other['done_when'])}"
        blocks.append(
            f"### {'Edit of existing issue ' if other['number'] is not None else 'Issue '}{key}"
            f"{': ' + other['title'] if other.get('title') else ''}\n"
            f"Lane: {other.get('lane') or '(from the tracker)'}\n"
            f"Goal: {other.get('goal') or '(unchanged)'}\n"
            f"Behaviour statements:\n{_lines(other.get('statements'))}"
            f"Out of scope: {other.get('out_of_scope') or '(unchanged)'}\n"
            f"Checklist:\n{_lines(other.get('checklist'))}"
            f"Blocked by: {', '.join(other.get('blocked_by') or []) or '(none)'}\n"
            f"Files expected to change: {', '.join(other.get('files') or []) or '(not listed)'}\n"
            f"{commands}{tests}")
    return "\n".join(blocks) or "No other issue in this order."
