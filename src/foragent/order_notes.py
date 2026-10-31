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


def entries(store) -> list[dict]:
    try:
        stream = (store.root / "notes.jsonl").open("rb")
    except FileNotFoundError:
        return []
    rows = []
    with stream:
        for line in stream:
            if not line.endswith(b"\n") or not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                # An interrupted single write can leave an incomplete record.
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def append(store, *, kind: str, boundary: str, content: str,
           replaces: str | None = None, language: str = "en") -> dict:
    if kind not in {"decision", "idea"} or not content.strip() or not boundary.strip():
        raise PlanError(text("order_notes_invalid", language))
    if replaces is not None and (kind != "decision" or not any(
            row.get("id") == replaces and row.get("kind") == "decision" for row in entries(store))):
        raise PlanError(text("order_notes_replaces_invalid", language))
    row = {"id": uuid4().hex, "kind": kind, "text": content, "boundary": boundary,
           "recorded_at": datetime.now(UTC).isoformat()}
    if replaces is not None:
        row["replaces"] = replaces
    # A leading separator isolates a partial tail without rewriting any earlier byte.
    payload = ("\n" + json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    store.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(store.root / "notes.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(fd, payload) != len(payload):
            raise OSError(text("order_notes_short_write", language))
        os.fsync(fd)
    finally:
        os.close(fd)
    return {**row, "status": "waiting"}


def pending(store) -> dict:
    return {"orders": [], "notes": [{**row, "status": "waiting"} for row in entries(store)]}


def run(args, root) -> int:
    from foragent.commands import facade, repo_store

    cli = facade()
    config, _repo, store = repo_store(root)
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    try:
        if args.action == "note":
            result = append(store, kind=args.kind, boundary=args.defer, content=args.text,
                            replaces=args.replaces, language=config.language)
            print(json.dumps(result, ensure_ascii=False) if args.json else
                  say("order_notes_saved", **{key: result[key] for key in ("id", "boundary")}))
        else:
            result = pending(store)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            elif not result["notes"]:
                print(say("order_notes_empty"))
            else:
                for row in result["notes"]:
                    print(say("order_notes_row", **{key: row[key] for key in ("id", "kind", "boundary", "text")}))
    except OSError as exc:
        raise cli.order.PlanError(say("order_notes_io", error=exc)) from exc
    return 0
