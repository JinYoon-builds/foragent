"""Observed GitHub fields: explicit reads refresh them, publishes only record writes."""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from foragent import order_references
from foragent.i18n import text
from foragent.issue import parse_body

FILE = ".foragent/order-observed.json"


def fields(issue) -> dict:
    return {"title": issue.title, "body": issue.body}


def _directory(root: Path, language: str, *, create: bool):
    from foragent.order import PlanError

    try:
        return order_references.real_directory(root, ".foragent", create=create)
    except ValueError as exc:
        raise PlanError(text("observed_invalid", language, path=FILE)) from exc


def _load(root: Path, language: str) -> dict:
    from foragent.order import PlanError

    if _directory(root, language, create=False) is None:
        return {}
    path = root / FILE
    if path.is_symlink():
        raise PlanError(text("observed_invalid", language, path=FILE))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(
            not isinstance(issues, dict) or any(
                not isinstance(value, dict) or set(value) != {"title", "body"}
                or any(not isinstance(v, str) for v in value.values()) for value in issues.values()
            ) for issues in data.values()
        ):
            raise ValueError
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise PlanError(text("observed_invalid", language, path=FILE)) from exc


def save(root: Path, identity: str, values: dict, language: str) -> None:
    if not values:
        return
    directory = _directory(root, language, create=True)
    data = _load(root, language)
    data.setdefault(identity, {}).update(values)
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        from foragent.order import PlanError
        raise PlanError(text("observed_invalid", language, path=str(ignore)))
    if not ignore.exists():
        with ignore.open("x", encoding="utf-8") as stream:
            stream.write("*\n")
    fd, name = tempfile.mkstemp(prefix="order-observed-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(name, root / FILE)
    finally:
        Path(name).unlink(missing_ok=True)


def context(issue, language: str) -> str:
    return "\n\n" + text("observed_context", language, number=issue.number,
                           title=issue.title, body=issue.body)


def _changes(old: dict, current: dict):
    if old["title"] != current["title"]:
        yield "title", old["title"], current["title"]
    if old["body"] != current["body"]:
        before, after = asdict(parse_body(old["body"])), asdict(parse_body(current["body"]))
        changed = False
        for field in before:
            if before[field] != after[field]:
                changed = True
                yield field, json.dumps(before[field], ensure_ascii=False), json.dumps(after[field], ensure_ascii=False)
        # Unstructured bodies and changes outside known fields still need a diagnostic.
        if not changed:
            yield "body", old["body"], current["body"]


def check(root: Path, identity: str, issues: list, language: str) -> None:
    from foragent.order import PlanError

    recorded = _load(root, language).get(identity, {})
    conflicts = []
    for issue in issues:
        old = recorded.get(str(issue.number))
        if old is None:
            continue  # Legacy plans without an observed base retain their publication path.
        changes = [text("observed_change", language, field=text("observed_field_" + field, language),
                        old=before, current=after) for field, before, after in _changes(old, fields(issue))]
        if changes:
            conflicts.append(f"#{issue.number}\n" + "\n".join(changes))
    if conflicts:
        raise PlanError(text("observed_refusal", language, changes="\n\n".join(conflicts)))
