"""Durable order publications, consumed only by the merge owner between batches.

The request holds the approved plan, bytes and preparation evidence. Red evidence is
renewed by prepare's independent reviewer on the landing commit; no script interprets
test output. A saved push intent lets a successor finish issue effects after a lost
push response, using publication's existing idempotent issue path.
"""

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from foragent import order, order_prepare, order_prepare_review, order_reviewed, orch_inbox
from foragent.git import GitError
from foragent.state import StateStore


def requests(store):
    return sorted((json.loads(path.read_text(encoding="utf-8"))
                   for path in (store.root / "order-publications").glob("*.json")),
                  key=lambda row: row["sequence"])


def pending(store):
    return [row for row in requests(store) if row["status"] in {"pending", "applying"}]


def needs_owner(store):
    return any(row["status"] in {"pending", "applying"}
               or row["status"] == "preparation" and not row.get("pm_request") for row in requests(store))


def status(store):
    return [{key: row[key] for key in ("id", "title", "status", "reason")}
            for row in pending(store)]


def _save(store, row):
    store._write_json(store.root / "order-publications" / f"{row['id']}.json", row)


def enqueue(plan, root, config, repo, publication, *, approve, approve_keys, approve_numbers):
    store = StateStore.for_repo(repo)
    payload = {"plan": plan, "repo": repo, "approve": approve, "approve_keys": approve_keys or [],
               "approve_numbers": approve_numbers or [], "target": config.base_branch,
               # Binary references travel as base64 in the plan itself.
               "files": {name: data for name, data in publication.files.items() if isinstance(data, str)},
               "prepared": publication.reviewed_bases,
               "authoring": {name: (root / name).read_text(encoding="utf-8")
                             for name in (order.PREREAD_FILE, order.order_observed.FILE)
                             if (root / name).is_file()}}
    fingerprint = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with store._state_lock():
        previous = requests(store)
        existing = next((row for row in previous if row["fingerprint"] == fingerprint
                         and row["status"] in {"pending", "applying"}), None)
        if existing is not None:
            return existing
        row = {**payload, "id": uuid4().hex, "fingerprint": fingerprint,
               "title": plan["title"], "status": "pending", "reason": None,
               "sequence": max((row["sequence"] for row in previous), default=0) + 1}
        _save(store, row)
    order.kick_tick(root)
    return row


class RedCheckFailed(order.PlanError):
    pass


def recheck(request, plan, root, config, git, publication):
    """Re-run the same measured commands and independent review as prepare, on HEAD.

    Review a copy of the admitted evidence, retaining the original review and writer
    identities. Legacy issues use their goal and Done when with the same reviewer;
    they have no behaviour-statement coverage to renew.
    """
    store = StateStore.for_repo(request["repo"])
    directory = store.root / "order-publications" / request["id"] / uuid4().hex
    directory.mkdir(parents=True)
    path = directory / "review.json"
    record = {"plan": plan["id"], "issues": deepcopy(request["prepared"])}
    items = [*plan["issues"], *(order.edit_view(item, publication.by_number[item["number"]], config)
                               for item in plan.get("edits", []))]
    for item in items:
        entry = record["issues"].setdefault(item["key"], {
            "written": order.order_collect.acceptance_files({"issues": [item]}, publication.files, config),
            "done_when": item["done_when"], "attempts": []})
        entry.update(base_revision=git.head_sha(),
                     scratch=str(directory / order_prepare._slug(item["key"]) / "writer" / "tree"))
    failures = []
    for item in items:
        # Other candidates must remain visible as reviewed while each issue is checked.
        local = deepcopy(record)
        order_prepare_review.review(plan, {"statements": [], **item}, root, config, git, path, local, store,
                                    files=publication.files)
        checked = local["issues"][item["key"]]
        request.setdefault("red_checks", {})[item["key"]] = checked
        if checked["status"] != "reviewed":
            failures.append(f"{item['key']}: {checked['reason']}")
    if failures:
        raise RedCheckFailed("; ".join(failures))


def _return_to_preparation(store, row):
    """Keep feedback in prepare's existing record without replacing a newer preparation."""
    path = order_prepare._record_path(store, row["plan"])
    with order_prepare._owner(path) as owned:
        if not owned:
            return
        record = order_prepare._read(path, row["plan"])
        for key, admitted in row["prepared"].items():
            current = record["issues"].get(key)
            if current is None or current != admitted:
                continue
            checked = row.get("red_checks", {}).get(key)
            if checked is None:
                current.update(status="interrupted", phase="reviewing_tests", reason=row["reason"])
            elif checked["status"] != "reviewed":
                record["issues"][key] = checked
        order_prepare._write(path, record)


def _notify(store, row):
    # The PM receives the existing durable inbox path, once even if the owner stopped
    # after creating the inbox entry but before saving its id on this request.
    notice = next((r for r in orch_inbox.requests(store) if r.get("publication") == row["id"]), None)
    if notice is None:
        notice = orch_inbox.add_request(store, "gate",
            f"Order {row['title']} returned to preparation: {row['reason']}",
            extra={"publication": row["id"]})
    row["pm_request"] = notice["id"]
    _save(store, row)


@contextmanager
def _checkout(ctx, target, row):
    """Owner writes its own detached tree; the caller's branch and edits stay untouched."""
    with TemporaryDirectory(prefix="order-publication-", dir=ctx.store.root) as directory:
        root = Path(directory) / "tree"
        ctx.git.add_detached_worktree(root, target)
        try:
            # Installed lane skills are local, so Git's detached tree alone does
            # not reproduce the context prepare binds. Use its existing copier.
            from foragent import order_prepare_setup
            from foragent.order_scratch_context import Context

            context = Context(ctx.repo_root)
            lanes = {item["lane"] for item in row["plan"]["issues"]}
            lanes.update(order.issue_lane(ctx.tracker.get_issue(item["number"]).labels, ctx.config.lanes)
                         for item in row["plan"].get("edits", []))
            for lane in lanes:
                order_prepare_setup._checkout_context(ctx.config, lane, ctx.repo_root, context)
            context.apply(root)
            if context.refused:
                raise order.PlanError("Could not reproduce preparation context: " + ", ".join(context.refused))
            git = ctx.git.at(root)
            # The file writer (including proposed lane installation) works on
            # this request's target. Its private checkout publishes detached HEAD.
            git.current_branch = lambda: row["target"]
            git.push_base = lambda branch: git._call(["push", "origin", f"HEAD:refs/heads/{branch}"])
            for name, data in row["authoring"].items():
                order.order_references.real_directory(root, ".foragent")
                order.check_write(root, name, ".foragent")
                (root / name).write_text(data, encoding="utf-8")
            yield root, git
        finally:
            ctx.git.remove_worktree(root)


def apply_next(ctx, report):
    """One request per owner pass, before batch creation, under merge-owner.lock.

    True reserves this pass for publication (including a deferred or refused attempt).
    False lets an existing batch progress or, with no request, admits the next batch.
    """
    from foragent.merge_owner import may_write_target, progressed

    for row in requests(ctx.store):
        if row["status"] == "preparation" and not row.get("pm_request"):
            _return_to_preparation(ctx.store, row)
            _notify(ctx.store, row)
    rows = pending(ctx.store)
    if not rows or not may_write_target(ctx, ctx.config.base_branch):
        return False
    row = rows[0]
    if row["target"] != ctx.config.base_branch:
        row.update(status="preparation", reason="Publication target changed; prepare for the configured base branch.")
        _save(ctx.store, row)
        _notify(ctx.store, row)
        return True
    writing = False
    try:
        ctx.git.fetch("origin", row["target"])
        target = ctx.git.ref_sha(f"origin/{row['target']}")
        landed = row.get("commit") and ctx.git.is_ancestor(row["commit"], target)
        writing = bool(landed)
        with _checkout(ctx, target, row) as (root, git):
            options = {key: row[key] for key in ("approve", "approve_keys", "approve_numbers")}
            if landed:
                with order_reviewed.publication_gate(ctx.repo):
                    publication = order._publication_checks(row["plan"], root, ctx.config, ctx.tracker,
                        repo=ctx.repo, git=git, retained_files=row["files"], landed=True, **options)
                    order._publish_issues(row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo,
                        publication=publication, published_blobs=git.blob_shas(row["commit"], publication.files),
                        approve=row["approve"], wake_root=ctx.repo_root)
            else:
                def intent(commit):
                    nonlocal writing
                    writing = True
                    row.update(status="applying", commit=commit)
                    _save(ctx.store, row)

                order.publish(row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo, git=git,
                              publication_request=row, before_push=intent, wake_root=ctx.repo_root, **options)
        row.update(status="published", reason=None)
        progressed()
    except order.PlanError as exc:
        row["reason"] = str(exc)
        if not writing:
            row["status"] = "preparation"
            _save(ctx.store, row)
            _return_to_preparation(ctx.store, row)
            _notify(ctx.store, row)
    except (GitError, OSError) as exc:
        row["reason"] = str(exc)
    _save(ctx.store, row)
    report.actions.append(f"Order publication {row['id']}: {row['status']}")
    return True
