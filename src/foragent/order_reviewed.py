"""Publication reads the reviewed evidence of `fa order prepare` (#1049).

Preparation owns the record (``order_prepare``); publication only reads it. A statement issue is
published only when the record holds a passing review of exactly the plan's current tests and
commands, for the issue's current cycle identity (requirements, shared contracts, references,
risk guidance, writer and reviewer setup), made on the base publication is about to write onto.
No model starts here and nothing is written: stale or missing evidence sends the person back to
``fa order prepare``. The scheduler still captures Approval snapshots and the publisher still owns
the published bytes; this is only the approval lock's input check.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from foragent.config import Config
from foragent.decisions import PARENT
from foragent.git import GitRepo
from foragent.i18n import text
from foragent.issue import has_behaviour


def statement_items(plan: dict, by_number: dict, config: Config) -> list[dict]:
    """The statement issues and edits, read as preparation read them (edits through the tracker)."""
    from foragent.order import edit_view

    items = [item for item in plan["issues"] if "statements" in item]
    items += [edit_view(item, by_number[item["number"]], config)
              for item in plan.get("edits", []) if "statements" in item]
    return items


def _record(plan: dict, repo: str | None) -> dict:
    """This order's preparation record (issue key -> entry), read only; empty when none exists."""
    from foragent.order_prepare import _read, _record_path
    from foragent.state import StateStore

    if not repo:
        return {}
    return _read(_record_path(StateStore.for_repo(repo), plan), plan)["issues"]


def _taken(record: dict) -> set[str]:
    """The keys a preparation record holds: those it has started on and those it claimed at its start."""
    claimed = record.get("claimed")
    return set(record["issues"]) | (set(claimed) if isinstance(claimed, list) else set())


def _prepared_numbers(repo: str | None, listed) -> set[int]:
    """Existing issues any order's preparation took on the statement route, whatever its outcome.

    Every order's record counts because a new plan id must not leave an issue's history behind: an
    edit is recorded as ``#N``, and a child under its key, which its order's parent maps to a number.
    """
    from foragent.order_prepare import _read, _record_path
    from foragent.state import StateStore

    if not repo:
        return set()
    store = StateStore.for_repo(repo)
    numbers = set()
    for path in (store.root / "prepare").glob("*.json"):
        numbers |= {int(key[1:]) for key in _taken(_read(path, {"id": path.stem}))
                    if re.fullmatch(r"#[1-9][0-9]*", key)}
    for issue in listed:
        for identity, mapping, _acceptance in PARENT.findall(issue.body)[-1:]:
            record = _taken(_read(_record_path(store, {"id": identity}), {"id": identity}))
            try:
                numbers |= {number for key, number in json.loads(mapping).items()
                            if key in record and type(number) is int}
            except (ValueError, AttributeError):
                continue  # A malformed map names no issue; publication refuses its own order's.
    return numbers


def _child_prepared(repo: str | None, body: str) -> bool:
    """Whether the order and key this issue's own marker names took the statement route.

    The publisher writes the marker into the child's body when it creates it, so this link stands
    even when that order's parent map was never written or cannot be read. The publisher's marker
    comes after the text, so an earlier one is quoted text.
    """
    from foragent.order import _CHILD_LINE
    from foragent.order_prepare import _read, _record_path
    from foragent.state import StateStore

    if not repo:
        return False
    for identity, key in _CHILD_LINE.findall(body)[-1:]:
        return key in _taken(_read(_record_path(StateStore.for_repo(repo), {"id": identity}), {"id": identity}))
    return False


def publication_gate(repo: str):
    """Every publication path holds this from its last check through its file and issue writes."""
    from foragent.order_prepare_owner import publication_gate as gate
    from foragent.state import StateStore

    return gate(StateStore.for_repo(repo).root / "prepare")


def _taken_here(plan: dict, repo: str | None) -> set[str]:
    from foragent.order_prepare import _read, _record_path
    from foragent.state import StateStore

    return _taken(_read(_record_path(StateStore.for_repo(repo), plan), plan)) if repo else set()


def require_route_kept(plan: dict, config: Config, repo: str | None, bodies: dict[str, str],
                       action: str, *, numbers: dict[str, int] | None = None, listed=()) -> None:
    """Refuse an issue or edit that left the statement route after taking it.

    The ``statements`` field selects the route and the route is kept once taken: an issue this order
    prepared (any outcome, a question too), an existing issue any order prepared (found by number, by
    its order's parent map or by its own child marker), or an existing issue published with a Behaviour section cannot drop its statements to fall back to a clean legacy
    preread. ``bodies`` maps issue keys (``#N`` for edits) to the current body of the issue they
    publish onto, ``numbers`` the same keys to issue numbers, and ``listed`` holds the issues whose
    parent maps name each order's children. A key new to a new order is a new issue, not a return.

    Preparation claims its keys under ``publication_gate`` before any agent starts, so a check made
    inside the gate cannot miss a preparation under way and none starts until the writes are done.
    """
    from foragent.order import PlanError

    keys = [item["key"] for item in plan["issues"] if "statements" not in item]
    keys += [f"#{item['number']}" for item in plan.get("edits", []) if "statements" not in item]
    if not keys:
        return
    record, numbers = _taken_here(plan, repo), numbers or {}
    prepared = _prepared_numbers(repo, listed) if any(key in numbers for key in keys) else set()
    kept = [key for key in keys if key in record or numbers.get(key) in prepared
            or has_behaviour(bodies.get(key) or "") or _child_prepared(repo, bodies.get(key) or "")]
    if kept:
        raise PlanError(text("reviewed_route_kept", _language(config), action=action, keys=", ".join(kept)))


def landing(git: GitRepo, branch: str, *, refreshed: bool) -> str:
    """The commit publication writes onto, which the reviewed evidence must have been made on.

    After the lock's ``update_base`` that is HEAD itself. Before it the remote is fetched here: HEAD
    fast-forwards to the remote branch unless it holds commits the remote lacks, which
    ``update_base`` keeps and publication would commit on.
    """
    from foragent.order_scratch import base_revision

    if refreshed:
        return git.head_sha()
    remote = base_revision(git, branch)
    return remote if git.is_ancestor("HEAD", remote) else git.head_sha()


def _language(config: Config) -> str:
    return (getattr(config, "language", None) or "en").lower().split("-")[0]


def require_reviewed_tests(plan: dict, root: Path, config: Config, repo: str,
                           items: list[dict], files: dict[str, str | bytes] | None = None) -> dict[str, dict]:
    """The reviewed record entry of each statement issue; refuses unless every one has current evidence.

    ``files`` is the manifest publication read once and will write. The evidence is checked against
    those bytes, never against a second read of the checkout that could differ from what is written.

    The base itself is checked by ``require_tested_base`` against the base actually published onto.
    """
    from foragent.order import PlanError
    from foragent.order_prepare_cycle import identity
    from foragent.order_prepare_setup import others_digest, published_digest

    if not items:
        return {}
    language = _language(config)
    full = {"issues": _record(plan, repo)}
    record = full["issues"]
    missing, stale, differ, bases, changed, changed_keys, alone = [], [], [], {}, [], [], []
    for item in items:
        key, entry = item["key"], record.get(item["key"])
        if not isinstance(entry, dict) or entry.get("status") != "reviewed":
            status = entry.get("status") if isinstance(entry, dict) else None
            missing.append(f"{key} ({status})" if status else key)
        elif entry.get("cycle") != identity(plan, item, config, root, files):
            # A published file (a legacy test or helper, the spec, a reference) changed since review.
            now, then = published_digest(plan, root, config, files), entry.get("published") or {}
            paths = sorted(name for name in now.keys() | then.keys() if now.get(name) != then.get(name))
            if paths:
                changed += paths
                changed_keys.append(key)
            else:
                stale.append(key)
        elif (entry.get("written") != item.get("acceptance_files")
              or entry.get("done_when") != item.get("done_when")):
            differ.append(key)
        elif entry.get("others") != others_digest(plan, full, item):
            # Reviewed without the other statement issues' tests that are published beside it.
            alone.append(key)
        else:
            bases[key] = entry
    problems = []
    if missing:
        problems.append(text("reviewed_missing", language, keys=", ".join(missing)))
    if stale:
        problems.append(text("reviewed_stale", language, keys=", ".join(stale)))
    if differ:
        problems.append(text("reviewed_differ", language, keys=", ".join(differ)))
    if alone:
        problems.append(text("reviewed_alone", language, keys=", ".join(alone)))
    if changed:
        problems.append(text("reviewed_bytes_changed", language, paths=", ".join(dict.fromkeys(changed)),
                             keys=", ".join(changed_keys)))
    if problems:
        raise PlanError(" ".join(problems))
    return bases


def require_reviewed_bytes(git: GitRepo, config: Config, items: list[dict], entries: dict[str, dict],
                           files: dict[str, str | bytes]) -> None:
    """Every file publication writes for a statement issue holds the bytes its reviewer checked.

    The manifest is what ``validate_plan`` collected: the plan's acceptance files and the tests its
    Done when commands name, the latter read from the checkout. A written test must equal the
    reviewed candidate, another statement issue's test the reviewed test it was reviewed beside
    (every statement issue passed the gate, so ``entries`` holds them), any other named file its copy on the reviewed base, which is
    the tree the reviewer's scratch copy held. Files publication does not write land as they are on the
    landing commit, which ``require_tested_base`` binds.
    """
    from foragent.issue import acceptance_paths
    from foragent.order import PlanError

    changed, keys = [], []
    for item in items:
        entry = entries.get(item["key"])
        if entry is None:
            continue
        written = entry.get("written") or {}
        names = set(item.get("acceptance_files") or {}) | set(acceptance_paths(item["done_when"], config.acceptance_dir))
        for name in sorted(names):
            if name not in files:
                continue
            published = files[name]
            published = published.decode("utf-8", "surrogateescape") if isinstance(published, bytes) else published
            beside = {path: data for key, other in entries.items() if key != item["key"]
                      for path, data in (other.get("written") or {}).items()}
            reviewed = (written[name] if name in written else beside[name] if name in beside
                        else git.file_at(entry.get("base_revision") or "", name))
            if published != reviewed:
                changed.append(name)
                keys.append(item["key"])
    if changed:
        raise PlanError(text("reviewed_bytes_changed", _language(config), paths=", ".join(dict.fromkeys(changed)),
                             keys=", ".join(dict.fromkeys(keys))))


def require_tested_base(git: GitRepo, config: Config, entries: dict[str, dict], current: str) -> None:
    """Refuse unless the tests were reviewed on exactly ``current``, the commit publication lands on
    (``landing``). Any other commit needs new red evidence, even one with the same files."""
    from foragent.order import PlanError

    moved = [key for key, entry in entries.items() if entry.get("base_revision") != current]
    if moved:
        raise PlanError(text("reviewed_base_moved", _language(config), keys=", ".join(moved),
                             base=current[:12], branch=config.base_branch))
