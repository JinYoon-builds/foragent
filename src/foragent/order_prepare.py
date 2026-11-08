"""`fa order prepare`: the issue's lane writes its tests before publication (#1047).

This command alone owns the preparation record and the scratch trees. The writer, started
with the same setup as preread for its lane and size, may only add test files and helpers
under the acceptance directory of a scratch copy of current main. An independent reviewer
then checks them (`order_prepare_review`); only reviewed bytes reach the plan file
(`order_prepare_cycle.promote`). The original checkout is never written here.

Record (``<state>/prepare/<order id>.json``)::

    {"plan": "<order id>", "issues": {"<key>": {
        "key", "status", "phase", "questions", "notes", "reason", "updated_at",
        "base_revision", "scratch", "cycle", "written": {path: text}, "done_when": [command],
        "ignored": [tool output path], "coverage", "red", "requirements",
        "attempts": [{"attempt", "role": "writer", "cycle", "started_at", "finished_at", "status",
                      "base_revision", "scratch", "executor", "model", "options", "log",
                      "exit_code", "timed_out", "session_id", "written", "done_when",
                      "skills_missing", "questions", "notes", "refused", "ignored", "reason"}]}}}
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from foragent import capacity, executors, order_prepare_locks as locks, order_prepare_owner as owners, order_prepare_setup as setup, order_scratch
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.order_prepare_prompt import writer_prompt
from foragent.state import StateStore
from foragent.state_logs import observing
from foragent.tracker import Tracker

WRITER_TIMEOUT_S = 30 * 60
_RECORD_WRITER: ContextVar[Callable | None] = ContextVar("prepare_record_writer", default=None)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    """A readable, collision-free file name: a cleaned prefix plus a hash of the exact value.

    Distinct order ids or issue keys ("7" and the edit "#7", "a/b" and "a-b") never share a
    record, lock or scratch tree.
    """
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")[:40] or "issue"
    return f"{readable}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:10]}"


def _record_path(store: StateStore, plan: dict) -> Path:
    return store.root / "prepare" / f"{_slug(plan['id'])}.json"


def path_phase(store: StateStore, plan: str, phase: str | None, *, publication: str | None = None,
               locked: bool = False, replace: bool = False) -> None:
    """One interval per order across preparation retries and publication requests.

    Preparation's content record can be replaced by a fresh attempt; its waiting
    interval ends only when the order actually enters the publication queue.
    """
    with observing("Phase", order=plan, phase=phase):
        _path_phase(store, plan, phase, publication=publication, locked=locked, replace=replace)


def _path_phase(store, plan, phase, *, publication, locked, replace):
    from foragent import order_publications

    path = store.root / "order-phases" / f"{_slug(plan)}.json"
    with nullcontext() if locked else store._state_lock():
        requests = [row for row in order_publications.requests(store) if row["plan"]["id"] == plan]
        if requests:
            latest = requests[-1]
            current_phase = {"pending": "order-publication", "applying": "order-publication",
                             "preparation": "order-preparation", "published": None}.get(latest["status"])
            if publication is not None and (publication != latest["id"] or phase != current_phase):
                return  # Queue ownership/status survive missing/failed accounting metadata.
            if publication is None and latest["status"] in {"pending", "applying"}:
                return
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = {}
        if previous and previous.get("publication") != publication and not replace:
            if publication is not None or previous.get("phase") not in {None, phase}:
                return  # Only enqueue can transfer an interval to a new request.
            if previous.get("phase"):
                publication = previous.get("publication")  # A prepare retry keeps its owner.
        now = store._now()
        if previous.get("updated_at") and now < datetime.fromisoformat(previous["updated_at"]):
            return  # An out-of-order checkpoint cannot move the interval backwards.
        if previous.get("phase"):
            store.record_phase(previous["phase"], previous["started_at"], at=now, locked=True,
                               order=plan, publication=previous.get("publication"))
        started = (previous["started_at"] if previous.get("phase") == phase
                   else now.isoformat())
        store._write_json(path, {"phase": phase, "started_at": started,
                                 "publication": publication, "order": plan, "updated_at": now.isoformat()})
        if phase and previous.get("phase") != phase:
            store.record_phase(phase, started, at=now, locked=True, order=plan, publication=publication)


def _read(path: Path, plan: dict) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"plan": plan["id"], "issues": {}}
    return record if isinstance(record, dict) and isinstance(record.get("issues"), dict) else {
        "plan": plan["id"], "issues": {}}


def _write(path: Path, record: dict) -> None:
    if (save := _RECORD_WRITER.get()) is not None:
        save(path, record)
        return
    _persist(path, record)


def _persist(path: Path, record: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def _owner(path: Path):
    """Hold this order's preparation; yields False when another prepare already does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+") as handle:
        try:
            from foragent.file_locks import LockTimeout, acquire

            acquire(handle.fileno(), path.with_name(path.name + ".lock"), timeout=0,
                    journal_root=path.parent.parent)
        except LockTimeout:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _envelope_text(message: str):
    """Every JSON value in an agent's reply: the whole reply, a fenced block, the outermost braces."""
    texts = [message.strip()]
    texts += re.findall(r"```(?:json)?\s*\n(.*?)```", message, re.DOTALL)
    if "{" in message and "}" in message:
        texts.append(message[message.index("{"):message.rindex("}") + 1])
    for candidate in texts:
        try:
            yield json.loads(candidate)
        except ValueError:
            continue


def _envelope(message: str) -> dict | None:
    """The writer's JSON object, or None when no reply has the agreed shape."""
    for value in _envelope_text(message):
        files, commands, questions = (value.get(key) if isinstance(value, dict) else None
                                      for key in ("acceptance_files", "done_when", "questions"))
        notes = value.get("notes", []) if isinstance(value, dict) else None
        # The reply names the files it wrote; their text is read from the working copy.
        # An older reply that also carries each file's text is accepted for its names only.
        if isinstance(files, dict) and all(isinstance(v, str) for v in files.values()):
            files = list(files)
        if (isinstance(files, list) and all(isinstance(name, str) for name in files)
                and isinstance(commands, list) and all(isinstance(c, str) for c in commands)
                and isinstance(questions, list) and all(isinstance(q, str) for q in questions)
                and isinstance(notes, list) and all(isinstance(note, str) for note in notes)):
            return value | {"acceptance_files": files, "notes": notes}
    return None


def _failure(result, config: Config) -> dict | None:
    """Why the writer did not finish normally (usage limit, timeout, nonzero exit), else None."""
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    if result.error_kind == "limit":
        return {"status": "waiting", "reason": say("prepare_limit")}
    if result.timed_out or result.exit_code != 0:
        return {"status": "interrupted", "reason": say(
            "prepare_failed", code=result.exit_code, timeout=", timed out" if result.timed_out else "",
            log=result.log_path)}
    return None


def _judge(result, failure: dict | None, tree: Path, git: GitRepo, manifest: dict, config: Config,
           protected: tuple[Path, ...], before: dict | None = None) -> dict:
    """Facts of one writer attempt: what it wrote, what was refused, and the resulting status.

    The tree is always inspected, also when the writer failed or stopped (``failure``): an
    offending path is a QUESTION either way, and partial candidates are kept as facts only.
    """
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    discarded = []
    candidates, refused, ignored = order_scratch.changes(
        git, tree, manifest, config.acceptance_dir, protected, before, discarded=discarded)
    written, questions = {}, [say("prepare_refused_path", path=name, directory=config.acceptance_dir)
                              for name in refused]
    for name, data in candidates.items():
        try:
            written[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            questions.append(say("prepare_not_text", path=name))
    facts = {"written": written, "refused": refused, "ignored": ignored, "done_when": [],
             "notes": [f"discarded stray file {name}" for name in discarded]}
    if questions:
        return facts | {"status": "question", "questions": questions, "reason": say("prepare_question")}
    if failure is not None:
        return facts | failure
    reply = _envelope(result.last_message or "")
    facts["done_when"] = reply["done_when"] if reply else []
    facts["notes"].extend(reply["notes"] if reply else [])
    if reply and reply["questions"]:
        return facts | {"status": "question", "questions": reply["questions"], "reason": say("prepare_question")}
    if reply is None:
        return facts | {"status": "interrupted", "reason": say("prepare_bad_reply")}
    differ = sorted(written.keys() ^ set(reply["acceptance_files"]))
    if differ:
        return facts | {"status": "interrupted", "reason": say("prepare_reply_mismatch", paths=", ".join(differ))}
    if not written:
        return facts | {"status": "interrupted", "reason": say("prepare_no_tests")}
    return facts | {"status": "written", "reason": say("prepare_written", count=len(written))}


def _write_tests(plan: dict, item: dict, root: Path, config: Config, git: GitRepo,
                 path: Path, record: dict, base: str, cycle: str, asked: str | None = None,
                 store: StateStore | None = None) -> None:
    """One writer attempt in a new scratch tree; the record is saved before and after the agent."""
    from foragent.order_prepare_cycle import behaviour, previous_tests, revision_notes
    from foragent.order_prepare_review import _place

    entries = record["issues"]
    entry = entries.setdefault(item["key"], {"key": item["key"], "attempts": []})
    previous = dict(entry)
    revision = revision_notes(entry)
    requirements = {name: deepcopy(item.get(name))
                    for name in ("goal", "statements", "out_of_scope", "checklist", "files")}
    attempt = len(entry["attempts"]) + 1
    home = path.parent / _slug(plan["id"]) / _slug(item["key"])
    # Earlier attempts' trees stay for inspection; each attempt gets its own new tree.
    tree, log = home / f"attempt-{attempt}" / "tree", home / f"attempt-{attempt}" / "writer.log"
    facts = {"attempt": attempt, "role": "writer", "cycle": cycle, "requirement": asked,
             "started_at": _now(), "base_revision": base, "requirements": requirements,
             "scratch": str(tree), "log": str(log)}
    entry["attempts"].append(facts)
    for stale in ("coverage", "red"):
        entry.pop(stale, None)
    entry.update(status="writing_tests", phase="writing_tests", questions=[], notes=[], reason=None, cycle=cycle,
                 requirement=asked, updated_at=facts["started_at"], base_revision=base, scratch=str(tree),
                 behaviour=behaviour(item), published=setup.published_digest(plan, root, config))
    _write(path, record)
    say = lambda key, **values: text(key, config.language, **values)  # noqa: E731
    failure = {"status": "interrupted", "reason": say("prepare_stopped")}
    manifest = before = result = protected = None
    try:
        # One resolution feeds the tree, the setup, the prompt and the permissions.
        lane_config, context = setup.lane_context(plan, item, config, root)
        context.files.update(setup.scratch_files(plan, item, root, config, record))
        # Writes must never reach the checkout, its Git directory or this command's state.
        protected = order_scratch.protected(git, root, path.parent.parent)
        manifest = order_scratch.build(git, base, tree, context)
        if context.refused:
            # The writer would start with other context than preread reads: ask, do not start.
            failure = {"status": "question", "reason": say("prepare_question"), "questions": [
                say("prepare_context_refused", path=name) for name in context.refused]}
            manifest = None  # no writer ran: the outcome is the refusal itself
            return
        # Candidates stay outside the main/context baseline: unchanged or revised, they are
        # still this issue's output. Main and peer files keep their existing read-only boundary.
        # A corrected version of the issue's own lock that the plan carries is a candidate too (#2558):
        # it leaves the main baseline before saved work is read, so a revision of it is kept.
        carried = locks.carried(item)
        for name in carried:
            manifest.pop(name, None)
        resumed = {name: content for name, content in previous_tests(previous, item, git, base).items()
                   if name not in manifest}
        _place(tree, resumed, carried)
        entry.pop("carried", None)
        entry["candidate_input"] = deepcopy(item.get("acceptance_files") or {})
        start = setup.writer_setup(lane_config, item, tree)
        options = start.options | setup.permissions(start.executor, tree, protected, config.acceptance_dir,
                                                    tree.parent)
        facts.update(executor=start.executor, model=start.model, options=start.options,
                     skills_missing=start.missing_skills)
        if start.missing_skills:
            failure = {"status": "waiting", "reason": say("prepare_missing_skills", skills=", ".join(start.missing_skills))}
            return
        _write(path, record)
        prompt = (start.notes + writer_prompt(plan, item, lane_config, root, tree, base,
                                              previous=previous | {"written": resumed})
                  + revision + start.memory_notice)
        before = order_scratch.stamps(tree, manifest)  # taken last, right before the writer starts
        admitted = False
        try:
            # The writer is one agent workload: it waits for the shared machine slot like preread.
            with capacity.hold_workload(store, config, kind="prepare", role=f"{item['key']} writer"):
                admitted = True
                result = executors.executor_for(start.executor).run(worktree=tree, prompt=prompt, model=start.model,
                                                          timeout_s=WRITER_TIMEOUT_S, log_path=log, **options)
        except TimeoutError as exc:
            if admitted:
                raise
            failure = {"status": "waiting", "reason": say("prepare_admission", error=" ".join(str(exc).split()))}
            return
        facts.update(exit_code=result.exit_code, timed_out=result.timed_out, session_id=result.session_id)
        failure = _failure(result, config)
    except (GitError, OSError, ValueError) as exc:
        failure = {"status": "interrupted", "reason": say("prepare_error", error=" ".join(str(exc).split())[:300])}
    finally:
        # Whatever stopped the writer, its tree is inspected once it exists.
        try:
            outcome = failure if manifest is None else _judge(
                result, failure, tree, git, manifest, config, protected, before)
        except (GitError, OSError, ValueError) as exc:
            outcome = {"status": "interrupted", "reason": say("prepare_error", error=" ".join(str(exc).split())[:300])}
        if result is not None:
            entry["requirements"] = requirements
        elif previous.get("written"):
            # No writer ran: its previous work and commands still describe the candidates.
            outcome.setdefault("written", previous["written"])
            outcome["done_when"] = previous.get("done_when", [])
        if outcome.get("done_when"):
            outcome["done_when"] = locks.kept_commands(item, outcome["done_when"], config.acceptance_dir)
        facts.update(outcome, finished_at=_now())
        entry.update(status=outcome["status"], questions=outcome.get("questions", []),
                     notes=outcome.get("notes", []),
                     reason=outcome["reason"], updated_at=facts["finished_at"],
                     written=outcome.get("written", {}), done_when=outcome.get("done_when", []),
                     ignored=outcome.get("ignored", []))
        entry.pop("locks", None)
        if (own := sorted(set(entry["written"]) & set(locks.carried(item)))):
            entry["locks"] = own  # existing locks this attempt replaces in the reviewer's tree
        if outcome["status"] in {"interrupted", "waiting"}:
            entry["phase"] = "writing_tests"
        _write(path, record)


def prepare(plan: dict, root: Path, config: Config, git: GitRepo, store: StateStore,
            tracker: Callable[[], Tracker] | None = None, *, status: bool = False,
            plan_path: Path | None = None) -> list[dict]:
    """Per-issue preparation facts; with ``status`` only the record is read, no agent starts.

    Reviewed tests and their commands are written into ``plan_path`` while this command still
    owns the order's preparation.
    """
    from foragent.order import PlanError, edit_view, validate_plan
    from foragent import order_prepare_beside
    from foragent.order_prepare_cycle import prepare_issue, promote, reconcile

    try:
        validate_plan(plan, root, config)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    items = list(plan["issues"])
    if any("statements" in item for item in plan.get("edits", [])) and not status:
        if tracker is None:
            raise PlanError("preparing edits needs the tracker for the existing issues")
        source = tracker()
        found = order_prepare_beside.existing(git, config) if git is not None else None
        items += [edit_view(item, source.get_issue(item["number"]), config, found) for item in plan["edits"]]
    else:
        items += [{**item, "key": f"#{item['number']}"} for item in plan.get("edits", [])]
    path = _record_path(store, plan)
    legacy = {"status": "legacy", "phase": "preread", "reason": text("prepare_legacy", config.language)}
    if status:
        record = _read(path, plan)
        return [owners.row(item["key"], record["issues"].get(item["key"]) if "statements" in item else legacy,
                           config, record.get("owner")) for item in items]
    with _owner(path) as owned:
        record = _read(path, plan) if owned else None
        if not owned or not owners.take(record, config):
            busy = {"status": "waiting", "phase": "writing_tests", "reason": text("prepare_busy", config.language)}
            return [owners.row(item["key"], busy if "statements" in item else legacy, config) for item in items]
        path_phase(StateStore(store.root, now=lambda: datetime.fromisoformat(_now())), plan["id"], "order-preparation")
        statement = batch = [item for item in items if "statements" in item and not locks.lock_only(item)]
        if not owners.claim(path.parent, record, [item["key"] for item in statement], lambda: _write(path, record)):
            busy = {"status": "waiting", "phase": "writing_tests", "reason": text("prepare_publishing", config.language)}
            return [owners.row(item["key"], busy if "statements" in item else legacy, config) for item in items]
        lock = threading.RLock()

        def run_item(item, local):
            # Workers own separate issue records. Peers' tests stay stable throughout a wave;
            # only this worker's entry is merged when it saves, never an old copy of its peers.
            def save(destination, updated):
                with lock:
                    if item["key"] in updated["issues"]:
                        record["issues"][item["key"]] = deepcopy(updated["issues"][item["key"]])
                    _persist(destination, record)

            token = _RECORD_WRITER.set(save)
            try:
                base = order_scratch.base_revision(git, config.base_branch)
                # Promotion waits until the wave's candidates have been reconciled together.
                prepare_issue(plan, item, root, config, git, path, local, base, store)
            finally:
                _RECORD_WRITER.reset(token)

        retried = set()
        waves = 2
        if reconcile(statement, record, config):
            _write(path, record)
        while batch and waves:
            waves -= 1
            # Give a conflicting candidate one writer revision in this invocation. A writer that
            # still returns conflicting bytes keeps that feedback for the next invocation.
            retried.update(item["key"] for item in batch
                           if (record["issues"].get(item["key"]) or {}).get("status") == "conflict")
            snapshots = [deepcopy(record) for item in batch]
            workers = min(len(batch), capacity.effective_test_slots(config.test_slots))
            if workers == 1:
                for item in batch:
                    run_item(item, deepcopy(record))
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(run_item, item, local) for item, local in zip(batch, snapshots)]
                    for future in futures:
                        future.result()
            if reconcile(statement, record, config):
                _write(path, record)
            batch = []
            for item in statement:
                entry = record["issues"].get(item["key"], {})
                if (entry.get("status") == "conflict" and item["key"] not in retried) or (
                    entry.get("status") == "reviewed" and entry.get("others") != setup.others_digest(plan, record, item)):
                    batch.append(item)
            # A writer revision adds another set of tests; allow its peers a review wave too.
            if any(record["issues"][item["key"]]["status"] == "conflict" for item in batch):
                waves += 1
        reviewed = [item for item in statement if (record["issues"].get(item["key"]) or {}).get("status") == "reviewed"]
        if plan_path is not None and reviewed:
            base = order_scratch.base_revision(git, config.base_branch)
            for item in reviewed:
                entry = record["issues"][item["key"]]
                if not promote(plan_path, item, entry, root, config, base):
                    entry.update(status="interrupted", phase="reviewing_tests",
                                 reason=text("prepare_plan_changed", config.language))
                    _write(path, record)
        return [owners.row(item["key"], record["issues"].get(item["key"]) if "statements" in item else legacy,
                           config) for item in items]
