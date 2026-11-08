"""Durable publications: the gate checks red facts outside the merge-owner lock.

Prepare owns review; a separate gate process certifies red facts for the exact landing set.
The owner reuses that set while its recorded pytest inputs stay unchanged, leaving renewal off-owner.
D85 lands certified sets once; failed combinations retry singly and its journal recovers issues.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from foragent import capacity, order, order_prepare, order_prepare_check, order_prepare_review, order_reviewed, orch_inbox
from foragent.git import GitError
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
    # Checking and landing need the same canonical request. Never wait for its
    # checker under the merge-owner lock; unrelated requests and batches proceed.
    lock = store.lock_acceptance_baseline(f"order-publication-{identity}", blocking=False)
    try:
        yield lock is not None
    finally:
        if lock is not None:
            lock.close()


def enqueue(plan, root, config, repo, publication, *, approve, approve_keys, approve_numbers, approval=None):
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
        existing = latest if latest is not None and (
            latest["status"] == "published" and latest["plan"] == plan
            or latest["status"] in {"pending", "applying"} and latest["fingerprint"] == fingerprint
        ) else None
        if existing is None:
            row = {**payload, "id": uuid4().hex, "fingerprint": fingerprint,
                   "title": plan["title"], "status": "pending", "reason": None,
                   "sequence": max((row["sequence"] for row in previous), default=0) + 1}
            # Retain the replacement before retiring anything it replaces.
            _save(store, row)
            order_prepare.path_phase(store, plan["id"], "order-publication", publication=row["id"], locked=True,
                                     replace=True)
        else:
            row = existing
        retired = []
        for older in same_order:
            if older["status"] in {"pending", "preparation"} and older["id"] != row["id"]:
                older.update(status="superseded",
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
    measured = [run for check in checks for field in ("measured", "previous_measured")
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
    items = [*plan["issues"], *(order.edit_view(item, publication.by_number[item["number"]], config)
                               for item in plan.get("edits", []))]
    failures = []
    checks = {}
    baselines = request.setdefault("baseline_failing_node_ids", {})
    from foragent import order_prepare_setup, order_scratch

    for item in items:
        key = item["key"]
        admitted = request["prepared"].get(key, {})
        written = admitted.get("written") or order.order_collect.acceptance_files(
            {"issues": [item]}, publication.files, config)
        lane_config, context = order_prepare_setup.lane_context(plan, item, config, root)
        context.files.update({name: data.encode("utf-8") if isinstance(data, str) else data
                              for name, data in (publication.files if combined_files is None else combined_files).items()})
        protected = order_scratch.protected(git, root, store.root)
        measured_at = directory / order_prepare._slug(key)
        measured_at.mkdir()

        def measure(base, name, candidates=None):
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
                    admitted.get("done_when", item["done_when"]), folder,
                    order_prepare_review.REVIEW_TIMEOUT_S, collect_node_ids=True)

        previous = (_lock_correction_files(item, publication.by_number[item["number"]], written, git, config)
                    if item.get("edit") else None)
        if previous is not None:
            checked = _recheck_lock_correction(item, admitted, previous, git, tracker, context, measure)
            checks[key] = checked
            if checked["reason"] is not None:
                failures.append(f"{key}: {checked['reason']}")
            continue

        expected = baselines.get(key, admitted.get("failing_node_ids"))
        prepared_base = admitted.get("base_revision")
        seeded = None
        if expected is None and prepared_base:
            seeded = measure(prepared_base, "prepared")
            _collected, reviewed, _nonred, seed_reason = _red_facts(seeded, context)
            if seed_reason is not None:
                checks[key] = {**deepcopy(admitted), "status": "hold", "phase": "writing_tests",
                               "reason": seed_reason, "measured": seeded}
                failures.append(f"{key}: {checks[key]['reason']}")
                continue
            expected = baselines[key] = sorted(reviewed)
        measured = seeded if seeded is not None and prepared_base == git.head_sha() else measure(git.head_sha(), "target")
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
    locked = order.acceptance_paths(item["done_when"], config.acceptance_dir)
    if not set(written) <= set(locked):
        return None
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


def _recheck_lock_correction(item, admitted, previous, git, tracker, context, measure):
    """Prove both versions on one immutable issue head through prepare's runner."""
    checked = {**deepcopy(admitted), "status": "reviewed", "reason": None}
    try:
        branch = f"foragent/issue-{item['number']}"
        issue_head = _issue_head(git, tracker, branch)
        head = issue_head["head"]
        checked.update(base_revision=head, issue_head=issue_head)
        measured = measure(head, "corrected")
        checked["measured"] = measured
        if _issue_head(git, tracker, branch) != issue_head:
            raise GitError("Issue head moved during the corrected measurement")
        collected, failing, reason = _correction_facts(measured, context, previous, passing=True)
        checked.update(collected_node_ids=sorted(collected), failing_node_ids=sorted(failing))
        if reason is None:
            measured = measure(head, "previous", previous)
            checked["previous_measured"] = measured
            if _issue_head(git, tracker, branch) != issue_head:
                raise GitError("Issue head moved during the previous measurement")
            _collected, _failing, reason = _correction_facts(measured, context, previous, passing=False)
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
    workers = max(1, min(len(rows), capacity.effective_test_slots(ctx.config.test_slots)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for action in pool.map(lambda row: _check_request(ctx, row["id"], target), rows):
            if action:
                report.actions.append(action)
    _certify_pending(ctx, report, target)


def _check_request(ctx, identity, target):
    if checked_on(ctx.git, _load(ctx.store, identity), target, tracker=ctx.tracker):
        return  # Fresh evidence needs no reservation that could make the owner skip it.
    with _request_owner(ctx.store, identity) as owned:
        if not owned:
            return
        row = _load(ctx.store, identity)
        if (row["status"] not in {"pending", "applying"} or row["target"] != ctx.config.base_branch
                or checked_on(ctx.git, row, target, tracker=ctx.tracker)):
            return
        if row.get("commit") and ctx.git.is_ancestor(row["commit"], target):
            return  # A saved intent already landed; only issue effects remain.
        fields = {}
        try:
            with _checkout(ctx, target, row) as (root, git):
                publication = order._publication_checks(
                    row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo, git=git, queued=True,
                    retained_files=row["files"], reviewed_record=row["prepared"],
                    **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")})
                files = dict(publication.files)
                for name in _install_lanes(row["plan"], root, git):
                    files[name] = (root / name).read_bytes()
                # Keep the existing collection/importer protection, outside the owner.
                with capacity.hold_test_slot(ctx.config.test_slots):
                    order.order_collect.check(row["plan"], files, root, ctx.config, git, base=target)
                recheck(row, row["plan"], root, ctx.config, git, publication,
                        tracker=ctx.tracker, combined_files=files)
            fields.update(red_check_commit=target,
                          red_check_files=_recorded_files(row["red_checks"].values()),
                          baseline_red_check_commit=row.get("baseline_red_check_commit", target), reason=None)
        except order.PlanError as exc:
            fields.update(status="preparation", reason=str(exc))
        except (GitError, OSError, ValueError) as exc:
            # Unknown outcomes retain the request for the next independent pass.
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
                flush._merge(files, publication.files, ctx.config.language)
                prepared.append((row, root, git, publication))
            if any(row["plan"].get("new_lanes") for row in rows):
                root, git = stack.enter_context(_landing_tree(ctx, target))
                paths = set().union(*(_install_lanes(row["plan"], root, git) for row in rows))
                files.update({name: (root / name).read_bytes() for name in paths})
            failures = []
            for row, root, git, publication in prepared:
                with capacity.hold_test_slot(ctx.config.test_slots):
                    order.order_collect.check(row["plan"], files, root, ctx.config, git, base=target)
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
                record["issues"][key] = checked
            else:
                continue
            current = record["issues"][key]
            attempts = current.setdefault("attempts", [])
            if not any(attempt.get("publication") == row["id"] for attempt in attempts):
                attempts.append({"attempt": len(attempts) + 1, "role": "publication-checker",
                                 "cycle": current.get("cycle"), "requirement": current.get("requirement"),
                                 "publication": row["id"], "status": current["status"], "reason": current["reason"],
                                 "finished_at": order_prepare._now()})
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
    # Only certified requests can qualify for broader train admission. Publications
    # add or replace their landing paths; the push checks the actual Git diff again.
    rows = [row for row in rows if may_write_target(
        ctx, row["target"], {("M", name) for name in landing_paths(row)}
        if row.get("red_check_commit") else None)]
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
        with journal.locked(ctx.store), order_reviewed.publication_gate(ctx.repo):
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
                    waiting.append(f"{row['id']} waits for merge-queued {'; '.join(under)}")
                elif checked_on(ctx.git, row, target, tracker=ctx.tracker):
                    fresh.append(dict(row, entry_id=row["id"], landing_target=target))
            if fresh:
                if any(row.get("red_check_single_only") for row in fresh):
                    fresh = fresh[:1]
                _land_fresh(ctx, report, target, fresh, issues=issues)
    return True, "; ".join(waiting) or None


def _land_fresh(ctx, report, target, fresh, *, issues=None):
    """Feed the admitted members to D85, retaining shared write failures for retry."""
    from foragent import order_flush as flush, order_flush_journal as journal, order_flush_recovery as recovery

    try:
        with _landing_tree(ctx, target) as (root, git), git.publish_lock():
            prepared, files, roots = flush._preflight(
                fresh, [], root, ctx.config, ctx.tracker, ctx.repo, git,
                check=lambda row: _apply(ctx, report, row),
                reject=lambda row, exc: _failure(ctx, report, row, exc), issues=issues)
            if prepared:
                if not _certified_on(git, [row for row, _publication in prepared], target, files,
                                     tracker=ctx.tracker):
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
                    return  # Staging took time; let the off-owner checker renew moved issue heads.
                batch = {"kind": "selected", "batch": uuid4().hex[:12], "boundary": "owner publications",
                         "orders": [], "notes": [], "paths": sorted(files),
                         "publications": [row["id"] for row, _publication in prepared]}
                journal.append(ctx.store, **batch)
                members = [row for row, _publication in prepared]
                try:
                    sha = flush._write(root, git, files, roots, recovery.title(batch))
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
        return order._publication_checks(
            row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo, git=git, queued=True,
            retained_files=row["files"], reviewed_record=row["prepared"],
            issues=getattr(ctx, "publication_issues", None),
            **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")})


def _failure(ctx, report, row, exc, *, writing=False):
    row["reason"] = str(exc)
    if not writing and isinstance(exc, (order.PlanError, ValueError, str)):
        row["status"] = "preparation"
        _save(ctx.store, row)
        order_prepare.path_phase(ctx.store, row["plan"]["id"], "order-preparation", publication=row["id"])
        _return_to_preparation(ctx.store, row)
        _notify(ctx.store, row)
    _save(ctx.store, row)
    report.actions.append(f"Order publication {row['id']}: {row['status']}")


def _finish_batch(ctx, report, target, batch, members, *, checkout=None, prepared=None, sha=None, recovering=False,
                  issues=None):
    from foragent import order_flush as flush, order_flush_journal as journal

    try:
        with ExitStack() as stack:
            root, git = checkout or stack.enter_context(_landing_tree(ctx, target))
            flush._finish(root, ctx.config, ctx.tracker, git, ctx.store, ctx.repo, batch,
                          journal.entries(ctx.store), prepared, sha,
                          reject=lambda row, exc: _failure(ctx, report, row, exc, writing=True),
                          wake_root=ctx.repo_root, recovering=recovering,
                          resume_check=lambda row: _resume_check(ctx, target, row, issues=issues), issues=issues,
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


def _resume_check(ctx, target, row, *, issues=None):
    with _checkout(ctx, target, row) as (root, _git):
        return order._publication_checks(
            row["plan"], root, ctx.config, ctx.tracker, repo=ctx.repo,
            retained_files=row.get("files"), landed=True, issues=issues,
            **{key: row[key] for key in ("approve", "approve_keys", "approve_numbers")})


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
