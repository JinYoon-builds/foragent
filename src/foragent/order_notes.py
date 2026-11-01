"""Append-only planning history, outside the shared checkout (#1262).

Planning owns this journal. Publication checkpoints belong to flush, never here.
No publication, flush or store lock is needed for a single O_APPEND write.
"""

from datetime import UTC, datetime
import json
import os
from uuid import uuid4

from foragent.i18n import text
from foragent.order import PlanError


def entries(store, journal: str = "notes.jsonl", *, with_offsets: bool = False) -> list[dict]:
    try:
        stream = (store.root / journal).open("rb")
    except FileNotFoundError:
        return []
    rows = []
    with stream:
        offset = 0
        for line in stream:
            start, offset = offset, offset + len(line)
            if not line.endswith(b"\n") or not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                # An interrupted single write can leave an incomplete record.
                continue
            if isinstance(row, dict):
                if with_offsets:
                    row["entry_id"] = f"{journal}:{start}"
                rows.append(row)
    return rows


def append(store, *, kind: str, boundary: str, content: str,
           replaces: str | None = None, replace: str | None = None, language: str = "en") -> dict:
    if kind not in {"decision", "idea", "rule"} or not content.strip() or not boundary.strip():
        raise PlanError(text("order_notes_invalid", language))
    if (kind == "rule" and (replace is None or not replace.strip()
                           or len(replace.splitlines()) != 1 or len(content.splitlines()) != 1)
            or kind != "rule" and replace is not None):
        raise PlanError(text("order_flush_rule_invalid", language))
    if replaces is not None and (kind != "decision" or not any(
            row.get("id") == replaces and row.get("kind") == "decision" for row in entries(store))):
        raise PlanError(text("order_notes_replaces_invalid", language))
    row = {"id": uuid4().hex, "kind": kind, "text": content, "boundary": boundary,
           "recorded_at": datetime.now(UTC).isoformat()}
    if replaces is not None:
        row["replaces"] = replaces
    if replace is not None:
        row["replace"] = replace
    append_entry(store, row, language=language)
    return {**row, "status": "waiting"}


def append_entry(store, row: dict, *, journal: str = "notes.jsonl", language: str = "en") -> None:
    """Append one durable record without taking publication, flush or store locks."""
    # A leading separator isolates a partial tail without rewriting any earlier byte.
    payload = ("\n" + json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    store.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(store.root / journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError(text("order_notes_short_write", language))
        os.fsync(fd)
    finally:
        os.close(fd)


def pending(store) -> dict:
    from foragent import order_flush_journal, order_submissions

    published = order_flush_journal.published_entries(store)
    return {"orders": order_submissions.pending(store),
            "notes": [{**row, "status": "published" if row["entry_id"] in published else "waiting"}
                      for row in entries(store, with_offsets=True)]}


def run(args, root) -> int:
    from foragent.commands import facade, repo_store

    cli = facade()
    config, _repo, store = repo_store(root)
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    try:
        if args.action == "note":
            result = append(store, kind=args.kind, boundary=args.defer, content=args.text,
                            replaces=args.replaces, replace=args.replace, language=config.language)
            print(json.dumps(result, ensure_ascii=False) if args.json else
                  say("order_notes_saved", **{key: result[key] for key in ("id", "boundary")}))
        else:
            result = pending(store)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif not result["notes"] and not result["orders"]:
                print(say("order_notes_empty"))
            else:
                for row in result["orders"]:
                    print(say("order_flush_order_row", boundary=row["boundary"], id=row["id"],
                              status=say(f"order_flush_status_{row['status']}")))
                for row in result["notes"]:
                    print(say("order_notes_row", **{key: row[key] for key in ("id", "kind", "boundary", "text")},
                              status=say(f"order_flush_status_{row['status']}")))
    except OSError as exc:
        raise cli.order.PlanError(say("order_notes_io", error=exc)) from exc
    return 0
