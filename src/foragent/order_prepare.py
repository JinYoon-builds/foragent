"""`fa order prepare`: the issue's lane writes its tests before publication (#1047).

This command alone owns the preparation record and the scratch trees. The writer, started
with the same setup as preread for its lane and size, may only add test files and helpers
under the acceptance directory of a scratch copy of current main. An independent reviewer
then checks them (`order_prepare_review`); only reviewed bytes reach the plan file
(`order_prepare_cycle.promote`). The original checkout is never written here.

Record (``<state>/prepare/<order id>.json``)::

    {"plan": "<order id>", "issues": {"<key>": {
        "key", "status", "phase", "questions", "reason", "updated_at",
        "base_revision", "scratch", "cycle", "written": {path: text}, "done_when": [command],
        "ignored": [tool output directory], "coverage", "red",
        "attempts": [{"attempt", "role": "writer", "cycle", "started_at", "finished_at", "status",
                      "base_revision", "scratch", "executor", "model", "options", "log",
                      "exit_code", "timed_out", "session_id", "written", "done_when",
                      "skills_missing", "questions", "refused", "ignored", "reason"}]}}}
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from foragent import capacity, executors, order_prepare_owner as owners, order_prepare_setup as setup, order_scratch
from foragent.config import Config
from foragent.git import GitError, GitRepo
from foragent.i18n import text
from foragent.order_prepare_prompt import writer_prompt
from foragent.state import StateStore
from foragent.tracker import Tracker

WRITER_TIMEOUT_S = 30 * 60


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


def _read(path: Path, plan: dict) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"plan": plan["id"], "issues": {}}
    return record if isinstance(record, dict) and isinstance(record.get("issues"), dict) else {
        "plan": plan["id"], "issues": {}}


def _write(path: Path, record: dict) -> None:
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
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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
    if "{" in message:
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
        if (isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
                and isinstance(commands, list) and all(isinstance(c, str) for c in commands)
                and isinstance(questions, list) and all(isinstance(q, str) for q in questions)):
            return value
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
    candidates, refused, ignored = order_scratch.changes(git, tree, manifest, config.acceptance_dir, protected, before)
    written, questions = {}, [say("prepare_refused_path", path=name, directory=config.acceptance_dir)
                              for name in refused]
    for name, data in candidates.items():
        try:
            written[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            questions.append(say("prepare_not_text", path=name))
    facts = {"written": written, "refused": refused, "ignored": ignored, "done_when": []}
    if questions:
        return facts | {"status": "question", "questions": questions, "reason": say("prepare_question")}
    if failure is not None:
        return facts | failure
    reply = _envelope(result.last_message or "")
    facts["done_when"] = reply["done_when"] if reply else []
    if reply and reply["questions"]:
        return facts | {"status": "question", "questions": reply["questions"], "reason": say("prepare_question")}
    if reply is None:
        return facts | {"status": "interrupted", "reason": say("prepare_bad_reply")}
    differ = sorted(name for name in written.keys() | reply["acceptance_files"].keys()
                    if written.get(name) != reply["acceptance_files"].get(name))
    if differ:
        return facts | {"status": "interrupted", "reason": say("prepare_reply_mismatch", paths=", ".join(differ))}
    if not written:
        return facts | {"status": "interrupted", "reason": say("prepare_no_tests")}
    return facts | {"status": "written", "reason": say("prepare_written", count=len(written))}


def _write_tests(plan: dict, item: dict, root: Path, config: Config, git: GitRepo,
                 path: Path, record: dict, base: str, cycle: str, asked: str | None = None,
                 store: StateStore | None = None) -> None:
    """One writer attempt in a new scratch tree; the record is saved before and after the agent."""
    from foragent.order_prepare_cycle import behaviour, revision_notes, same_requirement

    entries = record["issues"]
    entry = entries.setdefault(item["key"], {"key": item["key"], "attempts": []})
    revision = revision_notes(entry) if same_requirement(entry, cycle, asked) else ""
    attempt = len(entry["attempts"]) + 1
    home = path.parent / _slug(plan["id"]) / _slug(item["key"])
    # Earlier attempts' trees stay for inspection; each attempt gets its own new tree.
    tree, log = home / f"attempt-{attempt}" / "tree", home / f"attempt-{attempt}" / "writer.log"
    facts = {"attempt": attempt, "role": "writer", "cycle": cycle, "requirement": asked,
             "started_at": _now(), "base_revision": base,
             "scratch": str(tree), "log": str(log)}
    entry["attempts"].append(facts)
    for stale in ("coverage", "red"):
        entry.pop(stale, None)
    entry.update(status="writing_tests", phase="writing_tests", questions=[], reason=None, cycle=cycle,
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
        start = setup.writer_setup(lane_config, item, tree)
        options = start.options | setup.permissions(start.executor, tree, protected, config.acceptance_dir,
                                                    tree.parent)
        facts.update(executor=start.executor, model=start.model, options=start.options,
                     skills_missing=start.missing_skills)
        if start.missing_skills:
            failure = {"status": "waiting", "reason": say("prepare_missing_skills", skills=", ".join(start.missing_skills))}
            return
        _write(path, record)
        prompt = start.notes + writer_prompt(plan, item, lane_config, root, tree, base) + revision + start.memory_notice
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
        facts.update(outcome, finished_at=_now())
        entry.update(status=outcome["status"], questions=outcome.get("questions", []),
                     reason=outcome["reason"], updated_at=facts["finished_at"],
                     written=outcome.get("written", {}), done_when=outcome.get("done_when", []),
                     ignored=outcome.get("ignored", []))
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
    from foragent.order_prepare_cycle import prepare_issue

    try:
        validate_plan(plan, root, config)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    items = list(plan["issues"])
    if any("statements" in item for item in plan.get("edits", [])) and not status:
        if tracker is None:
            raise PlanError("preparing edits needs the tracker for the existing issues")
        source = tracker()
        items += [edit_view(item, source.get_issue(item["number"]), config) for item in plan["edits"]]
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
        statement = batch = [item for item in items if "statements" in item]
        # The route is taken before any agent starts. Under the publication lock, so no legacy
        # publication of these issues passes its last check without seeing this.
        with git.publish_lock():
            record["claimed"] = sorted({*record.get("claimed", []), *(item["key"] for item in statement)})
            _write(path, record)
        for _ in range(2):
            for item in batch:
                # Main is read again for each issue: an earlier issue's agents may have run for long.
                base = order_scratch.base_revision(git, config.base_branch)
                prepare_issue(plan, item, root, config, git, path, record, base, plan_path, store)
            # Issues reviewed before a later one's tests were written are reviewed again beside them.
            batch = [item for item in statement if (record["issues"].get(item["key"]) or {}).get("status") == "reviewed"
                     and record["issues"][item["key"]].get("others") != setup.others_digest(plan, record, item)]
        return [owners.row(item["key"], record["issues"].get(item["key"]) if "statements" in item else legacy,
                           config) for item in items]
