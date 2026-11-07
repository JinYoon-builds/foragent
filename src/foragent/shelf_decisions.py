"""The decisions place: decided, nothing ordered, until idle capacity hands them to the orchestrator.

When fewer issues run than ``target_concurrency``, the gate files one drafting request naming the
oldest decisions with no issue yet and writes one draft order per decision that has none. A decision
whose existing draft names Files that queued or running work holds waits; the orchestrator keeps all
judgment about what the orders say.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from foragent import decisions, orch_inbox, shelf, shelf_io

KIND = "decision-drafting"
BUSY = {"ready", "running"}


def _sections(ledger: str) -> dict[str, tuple[str, str]]:
    """Decision id -> (heading title, section text) from the ledger."""
    found = {}
    headings = list(decisions.HEADING.finditer(ledger))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(ledger)
        line = ledger[heading.start():end].splitlines()[0]
        title = re.sub(r"^### D[0-9]+\.\s*(\d{4}-\d{2}-\d{2}\s*)?", "", line).strip()
        found[heading[1]] = (title, ledger[heading.start():end].strip())
    return found


def _drafts(store) -> dict[str, list[list[str]]]:
    """Decision id -> the Files lists of draft orders that name it."""
    found: dict[str, list[list[str]]] = {}
    for path in sorted((store.root / "drafts").glob("*/plan.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(plan, dict):
            continue
        items = [item for item in plan.get("issues") or [] if isinstance(item, dict)]
        for identity in plan.get("decisions") or []:
            found.setdefault(identity, []).append([file for item in items for file in item.get("files") or []])
        for item in items:
            for identity in item.get("decisions") or []:
                found.setdefault(identity, []).append(list(item.get("files") or []))
    return found


def _busy_files(store) -> list[str]:
    from foragent.issue import parse_body

    running = {run.number for run in store.active_runs() if run.kind == "issue"}
    files = []
    for row in store.local_issues():
        if row.get("state") == "open" and (set(row.get("labels") or ()) & BUSY or row["number"] in running):
            files += parse_body(row.get("body") or "").files
    return files


def unordered(store, root: Path) -> list[tuple[str, str, str]]:
    """(id, title, text) of ledger decisions with no issue yet, oldest first."""
    try:
        ledger = (Path(root) / decisions.LEDGER).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return []
    sections = _sections(ledger)
    rows = [row["id"] for row in decisions.view(Path(root), store.local_issues(), ledger)
            if row["state"] == decisions.NOTHING_ORDERED]
    return [(identity, *sections.get(identity, ("", ""))) for identity in sorted(rows, key=lambda d: int(d[1:]))]


def _write_draft(store, identity: str, title: str, text: str) -> str:
    order_id = f"decision-{identity.lower()}"
    plan = {"id": order_id, "title": f"{identity}: {title}" if title else identity,
            "summary": f"Gate draft for {identity}, decided with no issue yet; the orchestrator writes its issues.",
            "spec_markdown": text + "\n", "decisions": [identity], "issues": []}
    shelf_io.atomic_write(store.root / "drafts" / order_id / "plan.json",
                          json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return order_id


def tick(ctx) -> dict | None:
    """File one drafting request while capacity is idle; returns the new request, if any."""
    store = ctx.store
    waiting = unordered(store, ctx.repo_root)
    shelf.members(store, "decisions", {identity: store._timestamp() for identity, _, _ in waiting})
    running = sum(1 for run in store.active_runs() if run.kind == "issue")
    idle = int(getattr(ctx.config, "target_concurrency", 12)) - running
    if idle <= 0 or not waiting:
        return None
    if any(row.get("kind") == KIND for row in orch_inbox.requests(store, pending=True)):
        return None  # One open drafting request at a time.
    from foragent import overlap_files

    drafts, busy = _drafts(store), _busy_files(store)
    # Files filter only a decision whose draft already names Files; others pass.
    taken = [item for item in waiting
             if not any(files and overlap_files.any_overlap(ctx, files, busy) for files in drafts.get(item[0], []))][:idle]
    if not taken:
        return None
    made = {identity: _write_draft(store, identity, title, text)
            for identity, title, text in taken if identity not in drafts}
    lines = [f"- {identity}: {title}" + (f" (gate draft: drafts/{made[identity]})" if identity in made else "")
             for identity, title, _ in taken]
    body = (f"Idle capacity: {running} of target {idle + running} issues running. Draft orders for the oldest "
            "decisions with no issue yet, then publish them with fa order:\n" + "\n".join(lines))
    return orch_inbox.add_request(store, "gate", body, {"kind": KIND, "decisions": [item[0] for item in taken]})


def safe_tick(ctx) -> None:
    try:
        tick(ctx)
    except Exception as exc:  # noqa: BLE001 - one failed check must not stop the tick.
        print(f"decision supply failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
