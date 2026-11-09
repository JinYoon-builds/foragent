"""Durable publications: the gate checks red facts outside the merge-owner turn.

Prepare owns review; a separate gate process certifies red facts for the exact landing set.
The owner reuses that set while its recorded pytest inputs stay unchanged, leaving renewal off-owner.
D85 lands certified sets once; failed combinations retry singly and its journal recovers issues.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from foragent import (capacity, order, order_prepare, order_prepare_beside, order_prepare_check, order_prepare_review,
                      orch_inbox, state_claims)
from foragent.git import GitError
from foragent import main_write_requests, order_publications_merge
from foragent.run_base_move import BaseMove, moved_paths
from foragent.state import StateStore
from foragent.tracker import TrackerError
from foragent.state_logs import observing


def requests(store):
    return sorted((json.loads(path.read_text(encoding="utf-8"))
                   for path in (store.root / "order-publications").glob("*.json")),
                  key=lambda row: row["sequence"])


def pending(store):
    return [row for row in requests(store) if row["status"] in {"pending", "applying"}]


def returned_publications(store, now):
    """Orders waiting on the orchestrator for over two hours since their latest return."""
    latest = {row["plan"]["id"]: row for row in requests(store)}
    waiting = []
    for identity, row in latest.items():
        if row["status"] != "preparation":
            continue
        try:
            returned = datetime.fromisoformat(row.get("returned_at"))
        except (TypeError, ValueError):
            continue  # Older rows have no recorded return time.
        if returned.tzinfo is not None and now - returned > timedelta(hours=2):
            waiting.append({"order": identity, "title": row["title"]})
    return waiting


def needs_owner(store):
    return any(row["status"] in {"pending", "applying"}
               or row["status"] == "preparation" and not row.get("pm_request") for row in requests(store))


def status(store):
    return [{key: row[key] for key in ("id", "title", "status", "reason")}
            for row in pending(store)]


def _save(store, row):
    store._write_json(store.root / "order-publications" / f"{row['id']}.json", row)


def _load(store, identity):
    return json.loads((store.root / "order-publications" / f"{identity}.json").read_text(encoding="utf-8"))


@contextmanager
def _request_owner(store, identity):
    # The durable intent excludes checkers/publishers without holding a file
    # lock during their GitHub reads, scratch commands or publication push.
    with state_claims.hold(store.root / f"order-publication-{identity}.intent") as owned:
        yield owned


def enqueue(plan, root, config, repo, publication, *, approve, approve_keys, approve_numbers,
            submission_revision=None, approval=None):
    store = StateStore.for_repo(repo)
    payload = {"plan": plan, "repo": repo, "approve": approve, "approve_keys": approve_keys or [],
               "approve_numbers": approve_numbers or [], "target": config.base_branch,
               **({"approval": approval} if approval is not None else {}),
               # Binary references travel as base64 in the plan itself.
               "files": {name: data for name, data in publication.files.items() if isinstance(data, str)},
               "prepared": publication.reviewed_bases,
               "authoring": {name: (root / name).read_text(encoding="utf-8")
                             for name in (order.PREREAD_FILE, order.order_observed.FILE)
                             if (root / name).is_file()}}
    fingerprint = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with store._state_lock():
        previous = requests(store)
        same_order = [row for row in previous if row["plan"]["id"] == plan["id"]]
        latest = same_order[-1] if same_order else None
        revisions = [row["submission_revision"] for row in same_order if "submission_revision" in row]
        replayed = submission_revision is not None and bool(revisions) and submission_revision <= max(revisions)
        existing = latest if latest is not None and (
            replayed or latest["status"] == "published" and latest["plan"] == plan
            or latest["status"] in {"pending", "applying"} and latest["fingerprint"] == fingerprint
        ) else None
        if existing is None:
            row = {**payload, "id": uuid4().hex, "fingerprint": fingerprint,
                   "title": plan["title"], "status": "pending", "reason": None,
                   "sequence": max((row["sequence"] for row in previous), default=0) + 1,
                   **({"submission_revision": submission_revision} if submission_revision is not None else {})}
            # Retain the replacement before retiring anything it replaces.
            _save(store, row)
            order_prepare.path_phase(store, plan["id"], "order-publication", publication=row["id"], locked=True,
                                     replace=True)
        else:
            row = existing
        if existing is not None and submission_revision is not None and not replayed:
            row["submission_revision"] = submission_revision
            _save(store, row)
        retired = []
        for older in same_order:
            if older["status"] in {"pending", "preparation"} and older["id"] != row["id"]:
                older.update(status="superseded", superseded_by=row["id"],
                             reason=f"Superseded by publication request {row['id']} for order {plan['id']}.")
                _save(store, older)
                retired.append(older)
    # Inbox writes take the same non-reentrant state lock, so close them after it.
    orch_inbox.close_publication_requests(
        store, retired, f"Superseded by publication request {row['id']} for order {plan['id']}.")
    if existing is not None:
        return existing
    order.kick_tick(root)
    return row


class RedCheckFailed(order.PlanError):
    pass


def checked_on(git, request, target, files=None, *, tracker=None):
    """Consume evidence on its target or a proved successor, with unchanged issue heads."""
    checked = request.get("red_check_commit")
    files = request["files"] if files is None else files
    on_target = checked == target
    if checked and not on_target:
        recorded = request.get("red_check_files")
        try:
            on_target = (recorded is not None and git.is_ancestor(checked, target)
                         and not moved_paths(git, BaseMove(checked, target)) & (set(recorded) | set(files)))
        except GitError:
            on_target = False
    return on_target and _heads_current(git, _correction_heads(request), tracker)


def _recorded_files(checks):
    """Union actual inputs of all commands, including both lock-correction measurements."""
    measured = [run for check in checks
                for field in ("measured", "previous_measured", "main_previous_measured", "main_measured")
                for run in check.get(field, [])]
    if not measured or any(run.get("loaded_files") is None for run in measured):
        return None  # Legacy or incomplete recordings must renew after a main move.
    return sorted({name for run in measured for name in run["loaded_files"]})


def _correction_heads(request):
    # Older correction records have previous_measured but no head binding: renew them.
    return {key: check.get("issue_head") if check.get("status") == "reviewed" else None
            for key, check in request.get("red_checks", {}).items()
            if "issue_head" in check or "previous_measured" in check}


def _issue_head(git, tracker, branch):
    """Read the selected PR and fetched branch together; disagreement proves no current head."""
    git.fetch("origin", branch)
    origin_head = git.ref_sha(f"origin/{branch}")
    pr = tracker.find_pr(branch) if tracker is not None else None
    head = git.ref_sha(tracker.pr_head_sha(pr)) if pr is not None else origin_head
    if head != origin_head:
        raise GitError(f"Issue PR and origin/{branch} heads differ")
    return {"branch": branch, "pr": pr, "head": head}


def _heads_current(git, heads, tracker):
    try:
        return all(expected is not None
                   and (expected["pr"] is None or tracker is not None)
                   and _issue_head(git, tracker, expected["branch"]) == expected
                   for expected in heads.values())
    except (GitError, TrackerError, OSError, ValueError):
        return False


def recheck(request, plan, root, config, git, publication, *, tracker=None, combined_files=None):
    """Renew only prepare's measured commands and structured red facts, off-owner."""
    store = StateStore.for_repo(request["repo"])
    directory = store.root / "order-publications" / request["id"] / uuid4().hex
    directory.mkdir(parents=True)
    found = order_prepare_beside.existing(git, config)
    items = [*plan["issues"], *(order.edit_view(item, publication.by_number[item["number"]], config, found)
                               for item in plan.get("edits", []))]
    failures = []
    checks = {}
    baselines = request.setdefault("baseline_failing_node_ids", {})
    from foragent import order_prepare_setup, order_scratch

    for item in items:
        key = item["key"]
        admitted = request["prepared"].get(key, {})
        landing = publication.files if combined_files is None else combined_files
        prepared_files = admitted.get("written") or order.order_collect.acceptance_files(
            {"issues": [item]}, publication.files, config)
        # Measure the bytes this publication lands, merged onto main's later changes (r1278).
        written = {name: landing[name] if isinstance(landing.get(name), str) else data
                   for name, data in prepared_files.items()}
        lane_config, context = order_prepare_setup.lane_context(plan, item, config, root)
        context.files.update({name: data.encode("utf-8") if isinstance(data, str) else data
                              for name, data in landing.items()})
        protected = order_scratch.protected(git, root, store.root)
        measured_at = directory / order_prepare._slug(key)
        measured_at.mkdir()

        def measure(base, name, candidates=None, commands=None):
            folder = measured_at / name
            folder.mkdir()
            measured_context = context
            if candidates is not None:
                measured_context = replace(context, files={**context.files, **{
                    name: data.encode("utf-8") for name, data in candidates.items()}})
            candidates = written if candidates is None else candidates
            with capacity.hold_test_slot(config.test_slots):
                return order_prepare_check.run(
                    git, base, measured_context, lambda copy: order_prepare_review._place(copy, candidates),
                    lambda copy, built, stamped: order_prepare_review._integrity(
                        git, copy, built, lane_config, protected, stamped, candidates),
                    admitted.get("done_when", item["done_when"]) if commands is None else commands, folder,
                    order_prepare_review.REVIEW_TIMEOUT_S, collect_node_ids=True)

        previous = (_lock_correction_files(item, publication.by_number[item["number"]], written, git, config)
                    if item.get("edit") else None)
        if previous is not None:
            checked = _recheck_lock_correction(item, admitted, previous, git, tracker, context, measure,
                                               written=written, config=config)
            checks[key] = checked
            if checked["reason"] is not None:
                failures.append(f"{key}: {checked['reason']}")
            continue

        expected = baselines.get(key, admitted.get("failing_node_ids"))
        prepared_base = admitted.get("base_revision")
        seeded = None
        if expected is None and prepared_base:
            seeded = measure(prepared_base, "prepared", prepared_files)  # The bytes preparation reviewed.
            _collected, reviewed, _nonred, seed_reason = _red_facts(seeded, context)
            if seed_reason is not None:
                checks[key] = {**deepcopy(admitted), "status": "hold", "phase": "writing_tests",
                               "reason": seed_reason, "measured": seeded}
                failures.append(f"{key}: {checks[key]['reason']}")
                continue
            expected = baselines[key] = sorted(reviewed)
        measured = (seeded if seeded is not None and prepared_base == git.head_sha() and written == prepared_files
                    else measure(git.head_sha(), "target"))
        checked = {**deepcopy(admitted), "base_revision": git.head_sha(), "measured": measured,
                   "status": "reviewed", "reason": None}
        checks[key] = checked
        # Preserve the measured diagnostic through preparation and its PM notice.
        # Structured pytest facts decide red; command output is only feedback.
        diagnostic = "\n".join(run["output"] for run in measured if run.get("output"))
        collected, failing, nonred, reason = _red_facts(measured, context)
        if reason is None:
            locked_files = order.order_collect.acceptance_files({"issues": [item]}, publication.files, config)
            locked = {node for node in collected if node.split("::", 1)[0] in locked_files}
            not_red = (locked - failing) | (locked & nonred)
            checked.update(failing_node_ids=sorted(failing), collected_node_ids=sorted(collected))
            if not locked or not_red or any(run["exit_code"] != 1 for run in measured):
                reason = diagnostic or "Locked tests no longer all fail: " + ", ".join(sorted(not_red or locked))
            if reason is None and expected is not None:
                missing, extra = set(expected) - failing, failing - set(expected)
                if extra:
                    extra -= _plan_added_ids(git, admitted.get("base_revision")
                                             or request.get("baseline_red_check_commit") or git.head_sha(),
                                             publication.files, extra, measured_at / "added", config)
                if missing or extra:
                    reason = ("Failing test node ids changed; missing: " + ", ".join(sorted(missing))
                              + "; added outside this plan: " + ", ".join(sorted(extra)))
            if reason is None:
                baselines[key] = sorted(failing) if expected is None else expected
        if reason is not None:
            checked.update(status="hold", phase="writing_tests", reason=reason)
            failures.append(f"{key}: {reason}")
    request["red_checks"] = checks
    if failures:
        raise RedCheckFailed("; ".join(failures))


def _lock_correction_files(item, issue, written, git, config):
    """Use preparation's exemption; only the existing test bytes remain to be read here."""
    if not order.order_prepare_locks.lock_only(item) or not written:
        return None
    beside = order.order_prepare_locks.beside(item)
    locked = order.acceptance_paths(item["done_when"], config.acceptance_dir)
    if not set(written) <= set(locked) | set(beside):
        return None
    if beside:
        # Always proved on the branch; missing or unchanged files are held there.
        return {name: git.file_at(git.head_sha(), name) for name in item.get("acceptance_files") or {}}
    previous = {name: git.file_at(git.head_sha(), name) for name in written}
    if any(data is None for data in previous.values()) or previous == written:
        return None
    return previous


def _correction_facts(measured, context, files, *, passing):
    """Require complete green commands or actual failures in the locked files."""
    collected, failing, nonred, reason = _red_facts(measured, context)
    if reason is None:
        locked = {node for node in collected if node.split("::", 1)[0] in files}
        nonpassing = {node for run in measured for node in run.get("nonpassing_node_ids", [])}
        proved = (locked and not failing and not locked & nonpassing
                  if passing else locked & (failing - nonred))
        if (not proved or any(run["exit_code"] != (0 if passing else 1)
                              or not run["collected_node_ids"] for run in measured)):
            reason = "\n".join(run["output"] for run in measured if run.get("output")) or (
                "Corrected locked tests must pass on the issue branch." if passing
                else "Previous locked tests must fail on the issue branch.")
    return collected, failing, reason


def _recheck_lock_correction(item, admitted, previous, git, tracker, context, measure, *, written=None,
                             config=None):
    """Prove both versions on one immutable issue head through prepare's runner.

    A correction beside the issue's own locks (r1210) is proved with ``order_prepare_beside.proof``
    and also measured on main, which decides whether it lands on main or on the issue branch."""
    from foragent import order_prepare_beside as beside

    checked = {**deepcopy(admitted), "status": "reviewed", "reason": None}
    side = order.order_prepare_locks.beside(item)
    commands, files = None, previous
    if side:
        # Only the files the edit carries, with the bytes publication will write.
        written = {name: written.get(name, item["acceptance_files"][name]) for name in previous}
        done_when = admitted.get("done_when", item["done_when"])
        commands, files = beside.proof(item, written, done_when, config.acceptance_dir)
    try:
        branch = f"foragent/issue-{item['number']}"
        issue_head = _issue_head(git, tracker, branch)
        head = issue_head["head"]
        checked.update(base_revision=head, issue_head=issue_head)
        reason = None
        if side:
            missing = sorted(name for name, data in previous.items() if data is None)
            same = sorted(name for name, data in previous.items() if data == written.get(name))
            if missing or same:
                reason = ("Corrected locked files must replace existing locked files on main; "
                          + "; ".join(part for part in (missing and "not on main: " + ", ".join(missing),
                                                        same and "unchanged: " + ", ".join(same)) if part))
        if reason is None:
            measured = measure(head, "corrected", commands=commands)
            checked["measured"] = measured
            if _issue_head(git, tracker, branch) != issue_head:
                raise GitError("Issue head moved during the corrected measurement")
            collected, failing, reason = _correction_facts(measured, context, files, passing=True)
            checked.update(collected_node_ids=sorted(collected), failing_node_ids=sorted(failing))
        if reason is None:
            measured = measure(head, "previous", previous, commands=commands)
            checked["previous_measured"] = measured
            if _issue_head(git, tracker, branch) != issue_head:
                raise GitError("Issue head moved during the previous measurement")
            _collected, _failing, reason = _correction_facts(measured, context, files, passing=False)
        if reason is None and side:
            target = git.head_sha()
            tests = beside.main_tests(git, target, written, done_when, config.acceptance_dir)
            before = measure(target, "main-previous", previous, commands=tests) if tests else []
            after = measure(target, "main-corrected", commands=tests) if tests else []
            checked.update(main_previous_measured=before, main_measured=after)
            red, reason = beside.turned_red(before, after)
            if reason is None:
                # Green on main before and not after: it lands with the issue's implementation.
                checked.update(main_turned_red=red, lands_on="branch" if red else "main",
                               branch_files=sorted(written) if red else [])
    except (GitError, TrackerError, OSError, ValueError) as exc:
        reason = f"Could not establish lock correction evidence on the issue branch: {exc}"
    if reason is not None:
        checked.update(status="hold", phase="writing_tests", reason=reason)
    return checked


def _red_facts(measured, context):
    """Require complete, unchanged pytest sessions before comparing node sets."""
    if (not measured or context.refused or order_prepare_check.changed(measured)
            or any(run["timed_out"] or run["exit_code"] not in {0, 1}
                   or run.get("collected_node_ids") is None or run.get("failing_node_ids") is None
                   or run.get("nonred_node_ids", []) is None
                   or run.get("nonpassing_node_ids", []) is None
                   for run in measured)):
        diagnostic = "\n".join(run["output"] for run in measured if run.get("output"))
        return set(), set(), set(), diagnostic or "Could not establish complete, unchanged pytest red evidence."
    return ({node for run in measured for node in run["collected_node_ids"]},
            {node for run in measured for node in run["failing_node_ids"]},
            {node for run in measured for node in run.get("nonred_node_ids", [])}, None)


def _plan_added_ids(git, base, files, nodes, directory, config):
    """Prove extra ids are additions by collecting the immutable pre-plan tree.

    Reuse the existing collection plugin and scratch runner, only when sets differ.
    Collect before and after overlaying the plan on the same immutable prepare base.
    """
    from foragent.run_baseline import COLLECT_PLUGIN

    paths = sorted({node.split("::", 1)[0] for node in nodes} & files.keys())
    if not paths:
        return set()
    existing = set(git._call(["ls-tree", "-r", "--name-only", "-z", base, "--", *paths]).split("\0")) - {""}
    directory.mkdir()

    def collect(names, overlay, name):
        if not names:
            return set()
        folder = directory / name
        folder.mkdir()
        listing = folder / "ids.json"
        try:
            code, tree, *_ = order.order_collect.run_copy(
                git, base, overlay, names, folder, config, config.language,
                time.monotonic() + order.order_collect.LIMIT_S, before="--collect-only", after="",
                conftest=order.order_collect._COPY_CONFTEST + COLLECT_PLUGIN.format(listing=str(listing)),
                timeout="Could not collect pre-plan test ids.", slot_timeout=None)
            facts = json.loads(listing.read_text(encoding="utf-8"))
        except (order.order_collect._Unavailable, OSError, ValueError) as exc:
            raise RedCheckFailed(f"Could not establish added test node ids: {', '.join(sorted(nodes))}") from exc
        if code not in {0, 5} or facts["errors"]:
            raise RedCheckFailed(f"Could not establish added test node ids: {', '.join(sorted(nodes))}")
        ids = set()
        for node in facts["ids"]:
            path, separator, rest = node.partition("::")
            try:
                path = (Path(facts["rootdir"]) / path).resolve().relative_to(tree.resolve()).as_posix()
            except ValueError as exc:
                raise RedCheckFailed(f"Collected test node id is outside the checked tree: {node}") from exc
            ids.add(path + separator + rest)
        return ids

    # Both collections use prepare's immutable base. A new parametrization caused
    # only by main moving is not a test this plan added, even in a carried file.
    before = collect(sorted(existing), {}, "before")
    after = collect(paths, files, "after")
    return nodes & (after - before)


def check_pending(ctx, report):
    """One off-owner pass; independent requests share only the machine test slots."""
    rows = pending(ctx.store)
    if not rows:
        return
    now = ctx.store._now()  # This pass needs only the store, Git and config of its context.
    if ctx.store.github_wait_until(now):
        from foragent import orch_github

        report_pending(ctx, report, orch_github.wait_text(ctx.store, now))
        return
    ctx.git.fetch("origin", ctx.config.base_branch)
    target = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
    # Resolve each request's publication context before admitting test commands.
    # The expensive measurements remain independent and parallel; their inputs
    # are immutable snapshots, and their request intents remain reserved.
    with ExitStack() as stack:
        work = [ready for row in rows
                if (ready := _prepare_request(ctx, row["id"], target, stack)) is not None]
        workers = max(1, min(len(work), capacity.effective_test_slots(ctx.config.test_slots)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for action in pool.map(lambda ready: _measure_request(ctx, target, ready), work):
                if action:
                    report.actions.append(action)
    _certify_pending(ctx, report, target)


def _prepare_request(ctx, identity, target, stack):
    if checked_on(ctx.git, _load(ctx.store, identity), target, tracker=ctx.tracker):
        return
    if not stack.enter_context(_request_owner(ctx.store, identity)):
        return
    row = _load(ctx.store, identity)
    if (row["status"] not in {"pending", "applying"} or row["target"] != ctx.config.base_branch
            or checked_on(ctx.git, row, target, tracker=ctx.tracker)):
        return
    if row.get("commit") and ctx.git.is_ancestor(row["commit"], target):
        return  # A saved intent already landed; only issue effects remain.
    try:
        root, git = stack.enter_context(_checkout(ctx, target, row))
        publication = order._publication_checks(
            row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo, git=git, queued=True,
            retained_files=row["files"], reviewed_record=row["prepared"],
            **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")})
        order_publications_merge.keep_main(git, row, target, publication)
        files = dict(publication.files)
        for name in _install_lanes(row["plan"], root, git):
            files[name] = (root / name).read_bytes()
        return row, (root, git, publication, files), None
    except (order.PlanError, GitError, OSError, ValueError) as exc:
        return row, None, exc


def _check_request(ctx, identity, target):
    with ExitStack() as stack:
        ready = _prepare_request(ctx, identity, target, stack)
        return _measure_request(ctx, target, ready) if ready is not None else None


def _measure_request(ctx, target, ready):
    row, prepared, error = ready
    identity = row["id"]
    fields = {}
    try:
        if error is not None:
            raise error
        root, git, publication, files = prepared
        with capacity.hold_test_slot(ctx.config.test_slots):
            order.order_collect.check(row["plan"], order_prepare_beside.collected_files(
                row["plan"], files, publication.by_number, ctx.config, git), root, ctx.config, git, base=target)
        recheck(row, row["plan"], root, ctx.config, git, publication,
                tracker=ctx.tracker, combined_files=files)
        fields.update(red_check_commit=target,
                      red_check_files=_recorded_files(row["red_checks"].values()),
                      baseline_red_check_commit=row.get("baseline_red_check_commit", target), reason=None)
    except order.PlanError as exc:
        fields.update(status="preparation", reason=str(exc), returned_at=ctx.store._timestamp())
    except (GitError, OSError, ValueError) as exc:
        return f"Order publication {identity}: red check deferred: {exc}"
    with ctx.store._state_lock():
        current = _load(ctx.store, identity)
        if current["status"] not in {"pending", "applying"}:
            return  # Enqueue superseded the snapshot while tests ran.
        for name in ("red_checks", "baseline_failing_node_ids"):
            if name in row:
                fields[name] = row[name]
        current.update(fields)
        _save(ctx.store, current)
        if current["status"] == "preparation":
            order_prepare.path_phase(ctx.store, current["plan"]["id"], "order-preparation",
                                     publication=current["id"], locked=True)
    if current["status"] == "preparation":
        _return_to_preparation(ctx.store, current)
        _notify(ctx.store, current)
    return f"Order publication {identity}: red check {current['status']} on {target}"


def _certified_on(git, rows, target, files, *, tracker=None):
    """Singleton checks compose only after measuring the same exact combined set."""
    if len(rows) == 1:
        return checked_on(git, rows[0], target, files, tracker=tracker)
    identities = sorted(row["id"] for row in rows)
    return bool(rows) and all(
        (certificate := row.get("red_check_set", {})).get("requests") == identities
        and certificate.get("issue_heads", {}).get(row["id"], {}) == _correction_heads(row)
        and checked_on(git, {**row, "red_check_commit": certificate.get("commit"),
                            "red_check_files": certificate.get("files")},
                       target, files, tracker=tracker)
        for row in rows)


def _certify_pending(ctx, report, target):
    """Measure every member with all landing bytes, retaining failed groups for single landings."""
    from foragent import order_flush as flush

    rows = [row for row in pending(ctx.store)
            if row["target"] == ctx.config.base_branch and not row.get("red_check_single_only")
            and checked_on(ctx.git, row, target, tracker=ctx.tracker)
            and not (row.get("commit") and ctx.git.is_ancestor(row["commit"], target))]
    files = {name: data for row in rows for name, data in row["files"].items()}
    if len(rows) < 2 or _certified_on(ctx.git, rows, target, files, tracker=ctx.tracker):
        return
    with ExitStack() as stack:
        for row in rows:
            if not stack.enter_context(_request_owner(ctx.store, row["id"])):
                return
        rows = [_load(ctx.store, row["id"]) for row in rows]
        if any(row["status"] not in {"pending", "applying"} or row.get("red_check_single_only")
               or row["target"] != ctx.config.base_branch
               or not checked_on(ctx.git, row, target, tracker=ctx.tracker)
               for row in rows):
            return
        try:
            prepared, files = [], {}
            for row in rows:
                root, git = stack.enter_context(_checkout(ctx, target, row))
                publication = order._publication_checks(
                    row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo, git=git, queued=True,
                    retained_files=row["files"], reviewed_record=row["prepared"],
                    **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")})
                order_publications_merge.keep_main(git, row, target, publication)
                flush._merge(files, publication.files, ctx.config.language)
                prepared.append((row, root, git, publication))
            if any(row["plan"].get("new_lanes") for row in rows):
                root, git = stack.enter_context(_landing_tree(ctx, target))
                paths = set().union(*(_install_lanes(row["plan"], root, git) for row in rows))
                files.update({name: (root / name).read_bytes() for name in paths})
            failures = []
            for row, root, git, publication in prepared:
                with capacity.hold_test_slot(ctx.config.test_slots):
                    order.order_collect.check(row["plan"], order_prepare_beside.collected_files(
                        row["plan"], files, publication.by_number, ctx.config, git), root, ctx.config, git, base=target)
                try:
                    recheck(row, row["plan"], root, ctx.config, git, publication,
                            tracker=ctx.tracker, combined_files=files)
                except RedCheckFailed as exc:
                    failures.append(f"{row['id']}: {exc}")
            if failures:
                raise RedCheckFailed("; ".join(failures))
            if not all(_heads_current(ctx.git, _correction_heads(row), ctx.tracker) for row in rows):
                raise RedCheckFailed("Issue head moved during the combined measurements")
            fields = {"red_check_set": {
                "requests": sorted(row["id"] for row in rows), "commit": target,
                "files": _recorded_files(check for row in rows for check in row["red_checks"].values())}}
            heads = {row["id"]: binding for row in rows if (binding := _correction_heads(row))}
            if heads:
                fields["red_check_set"]["issue_heads"] = heads
        except order.PlanError as exc:
            # Individually valid requests still belong in the queue. Their own
            # red checks certify single landings, renewed when the target moves.
            fields = {"red_check_single_only": True, "combined_red_check_reason": str(exc)}
        except (GitError, OSError, ValueError) as exc:
            report.actions.append(f"Order publication combined red check deferred: {exc}")
            return
        with ctx.store._state_lock():
            current = [_load(ctx.store, row["id"]) for row in rows]
            if any(row["status"] not in {"pending", "applying"} for row in current):
                return
            for row in current:
                row.update(fields)
                _save(ctx.store, row)
        report.actions.append("Order publication combined red check: "
                              + ("certified" if "red_check_set" in fields else "retry singly"))


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
                record["issues"][key] = deepcopy(checked)
            else:
                continue
            current = record["issues"][key]
            if admitted.get("status") == "reviewed":
                current["returned_reviewed"] = deepcopy(admitted)
            attempts = current.setdefault("attempts", [])
            if not any(attempt.get("publication") == row["id"] for attempt in attempts):
                attempts.append({"attempt": len(attempts) + 1, "role": "publication-checker",
                                 "cycle": current.get("cycle"), "requirement": current.get("requirement"),
                                 "publication": row["id"], "status": current["status"], "reason": current["reason"],
                                 "finished_at": order_prepare._now()})
        order_prepare._write(path, record)


def _notify(store, row):
    # One inbox entry belongs to the order across attempts, including recovery
    # after creating the entry but before saving its id on the publication.
    identity = row["plan"]["id"]
    with store.lock_acceptance_baseline(f"order-publication-notice-{identity}"):
        same_order = [r for r in requests(store) if r["plan"]["id"] == identity]
        publications = {r["id"] for r in same_order}
        # Recovery of an older attempt must not overwrite a newer return's reason.
        returned = next((r for r in reversed(same_order) if r["status"] == "preparation"), row)
        notice = next((r for r in orch_inbox.requests(store)
                       if r.get("order") == identity
                       or isinstance(r.get("publication"), str) and r["publication"] in publications), None)
        # Each return is one occurrence: a later return raises the same request again,
        # so a following orchestrator wakes for it; a recovery of this return does not.
        fields = {"order": identity, "publication": returned["id"],
                  "raised": f"{returned['id']}@{returned.get('returned_at') or ''}",
                  "text": f"Order {returned['title']} returned to preparation: {returned['reason']}"}
        if notice is None:
            notice = orch_inbox.add_request(store, "gate", fields["text"], extra=fields)
        else:
            notice = orch_inbox.raise_request(store, notice["id"], fields)
        with store._state_lock():
            current = _load(store, row["id"])
            current["pm_request"] = notice["id"]
            _save(store, current)
    if current["status"] != "preparation":
        orch_inbox.close_publication_requests(store, [current], current["reason"])


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


def apply_next(ctx, report, *, on_deferred=None):
    reason = None
    try:
        applied, reason = _apply_next(ctx, report)
        return applied
    except TrackerError as exc:
        reason = exc.diagnostic
        raise
    finally:
        if on_deferred is not None:
            on_deferred(reason)


def report_pending(ctx, report, reason=None):
    with observing("Flow", writer="publication"):
        _report_pending(ctx, report, reason)


def _report_pending(ctx, report, reason):
    if rows := pending(ctx.store):
        reasons = list(dict.fromkeys(row["reason"] for row in rows if row.get("reason")))
        why = reason or "; ".join(reasons) or "waiting for red-check certification on the current target"
        report.actions.append(f"Order publications not applied ({len(rows)} pending): {' '.join(why.split())}")
        for row in rows:
            order_prepare.path_phase(ctx.store, row["plan"]["id"], "order-publication", publication=row["id"])
            with observing("Flow", publication=row["id"]):
                ctx.store.record_flow("publication-wait", publication=row["id"], order=row["plan"]["id"], reason=why)


@contextmanager
def _pass_reads(ctx):
    from foragent.tracker import RecordingTracker

    previous = getattr(ctx, "publication_issues", None)
    tracker = ctx.tracker
    # Recovery and fresh intents can name the same order in one pass. Retain
    # every member's issue writes for the following member's targeted reads,
    # including callers that did not construct a RunContext themselves.
    ctx.tracker = RecordingTracker(tracker, ctx)
    ctx.publication_issues = order._PublicationReads(ctx.tracker, ctx.store)
    try:
        yield ctx.publication_issues
    finally:
        ctx.publication_issues = previous
        ctx.tracker = tracker


def _apply_next(ctx, report):
    """Land this pass's fresh requests through D85's single write and issue journal."""
    from foragent import order_flush_journal as journal, order_flush_recovery as recovery
    from foragent.merge_owner import may_write_target, overlaps, queued_paths

    for row in requests(ctx.store):
        if row["status"] == "preparation" and not row.get("pm_request"):
            with _request_owner(ctx.store, row["id"]) as owned:
                if owned:
                    row = _load(ctx.store, row["id"])
                    if row["status"] == "preparation" and not row.get("pm_request"):
                        _return_to_preparation(ctx.store, row)
                        _notify(ctx.store, row)
    rows = pending(ctx.store)
    if not rows:
        return False, None
    # Orders enter behind the running train. Certification can happen while it
    # runs, but publication waits for the owner's next available main turn.
    reserved = [row for row in rows if not may_write_target(ctx, row["target"])]
    for row in reserved:
        _wait(ctx, report, row, "target reserved by a merge train")
    rows = [row for row in rows if row not in reserved]
    if not rows:
        return False, "target reserved by a merge train"
    with _pass_reads(ctx) as issues, ExitStack() as locks:
        owned_rows = []
        for row in rows:
            if not locks.enter_context(_request_owner(ctx.store, row["id"])):
                continue
            with ctx.store._state_lock():
                row = _load(ctx.store, row["id"])
            if row["status"] in {"pending", "applying"}:
                owned_rows.append(row)
        if not owned_rows:
            return True, "publication requests are owned by another checker or publisher"
        try:
            ctx.git.fetch("origin", ctx.config.base_branch)
            target = ctx.git.ref_sha(f"origin/{ctx.config.base_branch}")
        except (GitError, OSError) as exc:
            for row in owned_rows:
                _failure(ctx, report, row, exc, writing=True)
            return True, str(exc)
        records = journal.entries(ctx.store)
        done = {entry["batch"] for entry in records if entry["kind"] == "done"}
        recovering = set()
        available = {row["id"] for row in owned_rows} | {
            row["id"] for row in requests(ctx.store) if row["status"] == "published"}
        for batch in records:
            if batch["kind"] != "selected" or "publications" not in batch or batch["batch"] in done:
                continue
            if not set(batch["publications"]) <= available:
                recovering.update(batch["publications"])
                continue  # Retain one recovery identity while a member's checker owns its lock.
            members = [_load(ctx.store, identity) for identity in batch["publications"]]
            if (batch.get("landed_commit") and ctx.git.is_ancestor(batch["landed_commit"], target)
                    or recovery._find(ctx.git, f"origin/{ctx.config.base_branch}", batch)):
                recovering.update(batch["publications"])
                _finish_batch(ctx, report, target, batch, members, sha=batch.get("landed_commit"),
                              recovering=bool(batch.get("landed_commit")), issues=issues)
            else:
                # An unpushed detached tree is disposable. The checker must
                # admit any new target before these members can be selected again.
                journal.append(ctx.store, "done", batch["batch"], abandoned=True)
        # Adopt push intents retained by the earlier single-request writer.
        # Their commit title predates D85's batch identity; issue recovery is
        # still the same journaled finish, with Git proving the push landed.
        legacy = {}
        for row in owned_rows:
            if (row["id"] not in recovering and row.get("commit")
                    and ctx.git.is_ancestor(row["commit"], target)):
                legacy.setdefault(row["commit"], []).append(row)
        for sha, members in legacy.items():
            batch = {"kind": "selected", "batch": uuid4().hex[:12], "boundary": "owner publications",
                     "orders": [], "notes": [], "paths": sorted({name for row in members for name in row["files"]}),
                     "publications": [row["id"] for row in members], "landed_commit": sha}
            journal.append(ctx.store, **batch)
            recovering.update(batch["publications"])
            _finish_batch(ctx, report, target, batch, members, sha=sha, recovering=True, issues=issues)
        try:
            queued = queued_paths(ctx, ctx.config.base_branch)
        except (GitError, OSError) as exc:
            return True, f"merge-queued member paths unreadable: {exc}"
        fresh, waiting = [], []
        for snapshot in owned_rows:
            row = _load(ctx.store, snapshot["id"])
            if row["id"] in recovering or row["status"] not in {"pending", "applying"}:
                continue
            if row["target"] != ctx.config.base_branch:
                _failure(ctx, report, row, "Publication target changed; prepare for the configured base branch.")
            elif under := [f"#{n} ({', '.join(names)})" for n, paths in sorted(queued.items())
                           if (names := overlaps(landing_paths(row), paths))]:
                # Landing now would put these files under the member's train tip.
                waiting.append(_wait(ctx, report, row, f"waits for merge-queued {'; '.join(under)}"))
            elif checked_on(ctx.git, row, target, tracker=ctx.tracker):
                fresh.append(dict(row, entry_id=row["id"], landing_target=target))
            else:
                # Main moved under its red check; the off-owner checker renews it, others land now.
                waiting.append(_wait(ctx, report, row, f"waits for red-check renewal on {target[:12]}"))
        if fresh:
            if any(row.get("red_check_single_only") for row in fresh):
                fresh = fresh[:1]
            _land_fresh(ctx, report, target, fresh, issues=issues)
    return True, "; ".join(waiting) or None


def _land_fresh(ctx, report, target, fresh, *, issues=None):
    """Feed the admitted members to D85, retaining shared write failures for retry."""
    from foragent import order_flush as flush, order_flush_journal as journal, order_flush_recovery as recovery

    try:
        with ExitStack() as reservations, _landing_tree(ctx, target) as (root, git):
            held = set()

            def check(row):
                publication = _apply(ctx, report, row)
                return publication if publication is not None and _reserve_route(
                    ctx, row, publication, reservations, held) else None

            prepared, files, roots = flush._preflight(
                fresh, [], root, ctx.config, ctx.tracker, ctx.repo, git,
                check=check,
                reject=lambda row, exc: _failure(ctx, report, row, exc), issues=issues)
            if prepared:
                # Corrections that would turn main red land on their issue branch, never on main (r1210).
                landings = order_prepare_beside.branch_landings(prepared)
                # The branch gets the bytes the red check measured, merged like main's (r1278).
                branch_files = {name: files.get(name, row["files"][name]) for row, _branch, _head, names in landings
                                for name in names}
                for _row, _branch, _head, names in landings:
                    for name in names:
                        files.pop(name, None)
                        roots.pop(name, None)
                if not _certified_on(git, [row for row, _publication in prepared], target, files,
                                     tracker=ctx.tracker):
                    for row, _publication in prepared:
                        _wait(ctx, report, row, "waits for red-check certification of this landing set")
                    return  # Membership changed; only the off-owner checker can certify it.
                # Claim all members together against enqueue's replacements.
                with ctx.store._state_lock():
                    if any(_load(ctx.store, row["id"])["status"] not in {"pending", "applying"}
                           for row, _publication in prepared):
                        return True
                    for row, _publication in prepared:
                        row.update(status="applying")
                        _save(ctx.store, row)
                try:
                    _stage_lanes(ctx, report, root, git, prepared, files, roots)
                except GitError:
                    return True  # The failed member was reported; others retry in a clean tree.
                if not _certified_on(git, [row for row, _publication in prepared], target, files,
                                     tracker=ctx.tracker):
                    for row, _publication in prepared:
                        _wait(ctx, report, row, "waits for red-check renewal of moved issue heads")
                    return  # Staging took time; let the off-owner checker renew moved issue heads.
                # Queued memory and note writes ride this commit, so the pass moves main once.
                riding, ride_files = main_write_requests.riders(ctx, target, set(files) | set(branch_files))
                for name, data in ride_files.items():
                    files[name], roots[name] = data, "."
                batch = {"kind": "selected", "batch": uuid4().hex[:12], "boundary": "owner publications",
                         "orders": [], "notes": [], "paths": sorted(files),
                         "publications": [row["id"] for row, _publication in prepared]}
                journal.append(ctx.store, **batch)
                members = [row for row, _publication in prepared]
                try:
                    for row, branch, head, names in landings:
                        order_prepare_beside.land_on_branch(
                            ctx.git, ctx.store.root, branch, head, {name: branch_files[name] for name in names},
                            f"Corrected locked tests for {branch} (order {row['plan']['id']})")
                    # The receipts let this pass's drain complete the riders without a commit.
                    message = recovery.title(batch) + (f"\n\n{main_write_requests.receipts(riding)}" if riding else "")
                    sha = flush._write(root, git, files, roots, message)
                    for row in members:
                        row.update(commit=sha)
                        _save(ctx.store, row)
                    _finish_batch(ctx, report, target, batch, members,
                                  checkout=(root, git), prepared=prepared, sha=sha, issues=issues)
                except (order.PlanError, GitError, OSError) as exc:
                    for row in members:
                        _failure(ctx, report, row, exc, writing=True)
    except (order.PlanError, GitError, OSError) as exc:
        for row in fresh:
            current = _load(ctx.store, row["id"])
            if current["status"] in {"pending", "applying"}:
                _failure(ctx, report, current, exc, writing=True)


def _apply(ctx, report, row):
    """Preflight one immutable admission in its own context; the flush writes all members."""
    if _load(ctx.store, row["id"])["status"] not in {"pending", "applying"}:
        return None
    target = row["landing_target"]
    with _checkout(ctx, target, row) as (root, git):
        return order_publications_merge.keep_main(git, row, target, order._publication_checks(
            row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo, git=git, queued=True,
            retained_files=row["files"], reviewed_record=row["prepared"],
            issues=getattr(ctx, "publication_issues", None),
            **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")}))


def _reserve_route(ctx, row, publication, stack, held):
    from foragent import order_reviewed

    if not stack.enter_context(order_reviewed.publication_reservation(
            row["plan"], ctx.repo, publication.mapping, publication.by_number, held=held)):
        return False
    # No claim can now pass this reservation. Re-read preparation's local
    # facts before the applying transition, independently of network preflight.
    order._require_route(row["plan"], ctx.config, ctx.repo, publication.mapping, publication.by_number)
    return True


def _wait(ctx, report, row, why):
    """Keep `row` queued for a later pass with its recorded reason; return the reported line."""
    with ctx.store._state_lock():
        current = _load(ctx.store, row["id"])
        if current["status"] in {"pending", "applying"} and current.get("reason") != why:
            current["reason"] = why
            _save(ctx.store, current)
    line = f"{row['id']} {why}"
    report.actions.append(f"Order publication {line}")
    return line


def _failure(ctx, report, row, exc, *, writing=False):
    with ctx.store._state_lock():
        current = _load(ctx.store, row["id"])
        if current["status"] not in {"pending", "applying"}:
            return
        current["reason"] = str(exc)
        if not writing and isinstance(exc, (order.PlanError, ValueError, str)):
            current.update(status="preparation", returned_at=ctx.store._timestamp())
        _save(ctx.store, current)
    row = current
    if row["status"] == "preparation":
        order_prepare.path_phase(ctx.store, row["plan"]["id"], "order-preparation", publication=row["id"])
        _return_to_preparation(ctx.store, row)
        _notify(ctx.store, row)
    report.actions.append(f"Order publication {row['id']}: {row['status']}")


def _finish_batch(ctx, report, target, batch, members, *, checkout=None, prepared=None, sha=None, recovering=False,
                  issues=None):
    from foragent import order_flush as flush, order_flush_journal as journal

    try:
        # A legacy landed intent may still be pending. Reserve its recovery
        # before external reads, just as a fresh landing reserves its members.
        with ctx.store._state_lock():
            current = [_load(ctx.store, row["id"]) for row in members]
            if any(row["status"] not in {"pending", "applying", "published"} for row in current):
                return
            for row in current:
                if row["status"] == "pending":
                    row["status"] = "applying"
                    _save(ctx.store, row)
        with ExitStack() as stack:
            held = set()
            root, git = checkout or stack.enter_context(_landing_tree(ctx, target))
            flush._finish(root, ctx.config, ctx.tracker, git, ctx.store, ctx.repo, batch,
                          journal.entries(ctx.store), prepared, sha,
                          reject=lambda row, exc: _failure(ctx, report, row, exc, writing=True),
                          wake_root=ctx.repo_root, recovering=recovering,
                          resume_check=lambda row: _resume_check(ctx, target, row, issues=issues,
                                                                reservations=stack, held=held), issues=issues,
                          owner=ctx)
    except (order.PlanError, GitError, OSError) as exc:
        for row in members:
            _failure(ctx, report, row, exc, writing=True)
    finally:
        completed = {entry["entry"] for entry in journal.entries(ctx.store)
                     if entry["kind"] == "issues" and entry["batch"] == batch["batch"]}
        for row in members:
            if row["id"] in completed:
                row = _load(ctx.store, row["id"])
                row.update(status="published", reason=None)
                _save(ctx.store, row)
                order_prepare.path_phase(ctx.store, row["plan"]["id"], None, publication=row["id"])
                report.actions.append(f"Order publication {row['id']}: published")
        from foragent import order_submissions

        # The original retained entries are published once their requests are; drop their plans.
        order_submissions.discard_published(ctx.store)


def _resume_check(ctx, target, row, *, issues=None, reservations, held):
    with _checkout(ctx, target, row) as (root, _git):
        publication = order._publication_checks(
            row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo,
            retained_files=row.get("files"), landed=True, issues=issues,
            **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")})
    if not _reserve_route(ctx, row, publication, reservations, held):
        raise GitError("Publication route is reserved by preparation or another publisher")
    return publication


def _landing_tree(ctx, target):
    # Each admission is preflighted separately with its own retained authoring
    # context. A bad first member must not prevent opening the shared writer.
    return _checkout(ctx, target, {"target": ctx.config.base_branch, "plan": {"issues": []}, "authoring": {}})


def _stage_lanes(ctx, report, root, git, prepared, files, roots):
    """Use the lane writer without committing ahead of the flush boundary."""
    paths = set()
    for row, _publication in prepared:
        try:
            paths.update(_install_lanes(row["plan"], root, git))
        except (ValueError, OSError, GitError) as exc:
            _failure(ctx, report, row, order.PlanError(str(exc)))
            raise GitError(str(exc)) from exc
    for name in paths:
        files[name] = (root / name).read_bytes()
        roots[name] = "."


def landing_paths(row):
    """Every repository path this request's landing commit can write.

    Text files travel in ``row["files"]``, binary references only in the plan, and
    new lanes add the files ``_install_lanes`` stages (``lanes.lane_files``).
    """
    from foragent import lane_memory

    plan = row["plan"]
    paths = set(row["files"]) | set(plan.get("reference_files", {}))
    for lane in plan.get("new_lanes", []):
        paths.update({".foragent.yml", f".claude/agents/fa-{lane['name']}.md", lane_memory.ATTRIBUTES})
        if lane.get("skills"):
            paths.add(".gitignore")
    return sorted(paths)


def _install_lanes(plan, root, git):
    """Materialize the lane writer's bytes for both certification and landing."""
    from foragent.config import load_config

    paths = set()
    if plan.get("new_lanes"):
        order._check_new_lanes(plan, root, load_config(root))
    for lane in plan.get("new_lanes", []):
        current = load_config(root)
        if lane["name"] not in current.lanes:
            order.add_lane(root, lane["name"], lane["paths"], skills=lane["skills"], git=git,
                           push=False, commit=False)
        paths.update(order.lane_files(root, lane["name"], load_config(root).lanes[lane["name"]]["skills"]))
    return paths
