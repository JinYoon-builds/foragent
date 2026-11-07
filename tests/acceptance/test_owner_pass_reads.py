"""A merge-owner publication pass reads open issues once and logs why it stopped.

Every pass here runs through the merge owner's public entry points: ``merge_owner.tick``
(one owner pass) and ``merge_owner.run_process`` (the real owner process, whose
stdout/stderr is merge-owner.log). GitHub is the offline ``FakeTracker`` of the
publication tests; Git is their offline ``OwnerGit``.

What counts as reading the issue list: a call to the tracker's ``list_issues`` (any
state) or ``list_open_issues``. "Paging every closed issue" is ``list_issues`` with
state ``all`` (the default) or ``closed``. A narrower read (for example
``issues_closed_since`` or a search) is allowed and not counted.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import os
import re

from test_order_publications import NAME, case, review_plan, submit, three_requests  # noqa: F401
from foragent import merge_owner, order_prepare_cycle, order_prepare_setup, order_publications
from foragent.scheduler import TickReport
from foragent.state import StateStore
from foragent.tracker import TrackerError

CLOSED_HISTORY = 240  # Closed issues of earlier orders; the real repository had 2,400+.
LISTING = {"list_issues", "list_open_issues"}


class Reads:
    """Record every issue-list read the tracker serves while armed."""

    def __init__(self, tracker, monkeypatch):
        self.calls, self.armed = [], False
        for name in (*LISTING, "issues_closed_since"):
            original = getattr(tracker, name)

            def recorded(*args, _name=name, _original=original, **kwargs):
                if self.armed:
                    state = (args[0] if args else kwargs.get("state", "all")) if _name == "list_issues" else (
                        "open" if _name == "list_open_issues" else "closed-since")
                    self.calls.append((_name, state))
                return _original(*args, **kwargs)

            monkeypatch.setattr(tracker, name, recorded)

    def listings(self):
        return [call for call in self.calls if call[0] in LISTING]

    def every_closed(self):
        return [call for call in self.calls if call[0] == "list_issues" and call[1] in {"all", "closed"}]


def closed_history(tracker, count=CLOSED_HISTORY):
    for index in range(count):
        number = tracker.create_issue(f"Earlier work {index}", f"Done long ago {index}\n", ["main", "s"])
        tracker.close_issue(number, reason="completed")


def parents(tracker, plan_id="publication"):
    marker = f"<!-- foragent:plan id={plan_id} map="
    return [issue for issue in tracker.list_issues("all") if marker in issue.body]


def owner_tick(case, reads):
    reads.armed = True
    try:
        merge_owner.tick(case.ctx, TickReport())
    finally:
        reads.armed = False


def statuses(case):
    return [row["status"] for row in order_publications.requests(case.store)]


# --- A publication pass's lock check does not read closed issues. -------------------------

def locking_owner(tracker, path=NAME):
    body = ("## Goal\nOther work\n\n## Out of scope\nNothing\n\n## Done when\n"
            f"```sh\nuv run pytest {path}\n```\n")
    return tracker.create_issue("Approved owner of the locked test", body, ["approved", "main", "s"])


def test_lock_check_refuses_from_open_issues_without_reading_closed_ones(case, monkeypatch):
    closed_history(case.tracker)
    owner = locking_owner(case.tracker)
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    reads = Reads(case.tracker, monkeypatch)

    owner_tick(case, reads)

    row, = order_publications.requests(case.store)
    # The refusal is unchanged: the open approved owner's locked test still stops the order.
    assert row["status"] != "published", "the lock check let a locked test change through"
    assert f"#{owner}" in (row["reason"] or ""), row["reason"]
    assert not parents(case.tracker), "a refused order created issues"
    assert reads.calls, "the lock check read no issues at all"
    assert not reads.every_closed(), f"the publication pass read every closed issue: {reads.calls}"


def test_lock_check_without_owners_reads_only_open_issues(case, monkeypatch):
    closed_history(case.tracker)
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    reads = Reads(case.tracker, monkeypatch)

    owner_tick(case, reads)

    assert statuses(case) == ["published"]
    assert len(parents(case.tracker)) == 1
    assert not reads.every_closed(), f"the publication pass read every closed issue: {reads.calls}"


# --- Publishing an order whose parent already exists finds it without paging every closed issue.

def published_once(case):
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    assert statuses(case) == ["published"]
    parent, = parents(case.tracker)
    return parent


def republish(case, summary="Revised order text"):
    case.plan["summary"] = summary
    review_plan(case)
    submit(case)
    order_publications.check_pending(case.ctx, TickReport())


def test_republication_finds_existing_parent_without_paging_closed_issues(case, monkeypatch):
    parent = published_once(case)
    closed_history(case.tracker)
    republish(case)
    reads = Reads(case.tracker, monkeypatch)

    owner_tick(case, reads)

    assert statuses(case)[-1] == "published", [r["reason"] for r in order_publications.requests(case.store)]
    found = parents(case.tracker)
    assert [issue.number for issue in found] == [parent.number], "the republication created a second parent"
    assert "Revised order text" in case.tracker.get_issue(parent.number).body
    assert not reads.every_closed(), f"finding the parent paged every closed issue: {reads.calls}"


def test_republication_finds_parent_among_many_closed_orders(case, monkeypatch):
    parent = published_once(case)
    # Closed parents of other orders carry plan markers too; none of them is this order's.
    for index in range(30):
        number = case.tracker.create_issue(
            f"Old order {index}", f"Old\n\n<!-- foragent:plan id=old-{index} map={{}} -->\n", ["main"])
        case.tracker.close_issue(number, reason="completed")
    closed_history(case.tracker)
    republish(case, "Second revision")
    reads = Reads(case.tracker, monkeypatch)

    owner_tick(case, reads)

    assert statuses(case)[-1] == "published"
    assert [issue.number for issue in parents(case.tracker)] == [parent.number]
    assert "Second revision" in case.tracker.get_issue(parent.number).body
    assert not reads.every_closed(), f"finding the parent paged every closed issue: {reads.calls}"


# --- A pass with several pending publications reads the issue list at most once. ----------

def test_pass_with_several_publications_reads_issue_list_at_most_once(case, monkeypatch):
    closed_history(case.tracker)
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    reads = Reads(case.tracker, monkeypatch)

    owner_tick(case, reads)

    rows = order_publications.requests(case.store)
    assert [r["status"] for r in rows] == ["published"] * 3, [r["reason"] for r in rows]
    for plan_id in ("publication", "second", "third"):
        assert len(parents(case.tracker, plan_id)) == 1, plan_id
    assert len(reads.listings()) <= 1, f"one pass read the issue list {len(reads.listings())} times: {reads.calls}"


def test_pass_with_several_republications_reads_issue_list_at_most_once(case, monkeypatch):
    three_requests(case)
    order_publications.check_pending(case.ctx, TickReport())
    merge_owner.tick(case.ctx, TickReport())
    assert statuses(case) == ["published"] * 3
    before = {plan_id: [issue.number for issue in parents(case.tracker, plan_id)]
              for plan_id in ("publication", "second", "third")}
    closed_history(case.tracker)
    for row in order_publications.requests(case.store):
        row = dict(row)
        row["plan"] = dict(row["plan"], summary=f"Revised {row['plan']['id']}")
        row.update(id=f"{row['id']}-again", sequence=row["sequence"] + 10, status="pending", reason=None)
        for key in ("commit", "red_check_commit", "red_checks"):
            row.pop(key, None)
        # Retain the fake review for the revised plan, as a fresh preparation would.
        item = row["plan"]["issues"][0]
        row["prepared"] = deepcopy(row["prepared"])
        row["prepared"]["new"].update(
            cycle=order_prepare_cycle.identity(row["plan"], item, case.config, case.root, row["files"]),
            published=order_prepare_setup.published_digest(row["plan"], case.root, case.config, row["files"]))
        order_publications._save(case.store, row)
    order_publications.check_pending(case.ctx, TickReport())
    reads = Reads(case.tracker, monkeypatch)

    owner_tick(case, reads)

    rows = order_publications.requests(case.store)
    assert [r["status"] for r in rows[3:]] == ["published"] * 3, [r["reason"] for r in rows[3:]]
    for plan_id, numbers in before.items():
        assert [issue.number for issue in parents(case.tracker, plan_id)] == numbers, plan_id
        assert f"Revised {plan_id}" in case.tracker.get_issue(numbers[0]).body
    assert len(reads.listings()) <= 1, f"one pass read the issue list {len(reads.listings())} times: {reads.calls}"
    assert not reads.every_closed(), reads.calls


# --- When an owner pass ends with pending publications not applied, merge-owner.log says why.

def owner_process(case, monkeypatch):
    """Run the real owner process entry point with offline transports.

    As merge_owner.start does with Popen, the process's stdout/stderr is
    merge-owner.log and it holds the inherited owner lock. Nothing is copied into
    the log by the test: run_process must emit it.
    """
    import foragent.git
    import foragent.scheduler_config
    import foragent.tracker

    monkeypatch.setattr(foragent.tracker, "GhTracker", lambda repo, *a, **kw: case.tracker)
    monkeypatch.setattr(foragent.git, "GitRepo", lambda root, *a, **kw: case.git)
    monkeypatch.setattr(foragent.scheduler_config, "fetch_base", lambda ctx, *a, **kw: None)
    store = StateStore(case.store.root)
    log = store.root / "merge-owner.log"
    before = log.read_text(encoding="utf-8") if log.exists() else ""
    lock = store.lock_acceptance_baseline("merge-owner", blocking=False)
    assert lock is not None
    with lock, log.open("a") as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            merge_owner.run_process({
                "repo": case.config.repo, "repo_root": str(case.root), "store_root": str(store.root),
                "config": asdict(case.config), "committed_config": False,
            }, os.dup(lock.fileno()))
        except TrackerError:
            pass  # An owner process that dies still leaves what it logged.
    return log.read_text(encoding="utf-8")[len(before):]


def why_lines(output):
    return [line for line in output.splitlines() if re.search(r"publication", line, re.I)]


def two_pending(case):
    three_requests(case)
    third = order_publications.requests(case.store)[-1]
    third.update(status="superseded", reason="not part of this case")
    order_publications._save(case.store, third)
    assert len(order_publications.pending(case.store)) == 2


def timing_out(tracker, monkeypatch):
    """Every GitHub read times out once armed, as gh does when the time budget is spent."""
    armed = []

    def failing(name):
        original = getattr(tracker, name)

        def call(*args, **kwargs):
            if armed:
                raise TrackerError("gh timed out after 30 seconds: gh api repos/owner/repo/issues",
                                   transport=True)
            return original(*args, **kwargs)
        return call

    for name in ("list_issues", "list_open_issues", "issues_closed_since", "get_issue", "parent_of",
                 "create_issue", "update_issue", "edit_body", "blocked_by", "list_comments"):
        if hasattr(tracker, name):
            monkeypatch.setattr(tracker, name, failing(name))
    return armed


def test_pass_that_runs_out_of_github_time_logs_one_line_why(case, monkeypatch):
    two_pending(case)
    order_publications.check_pending(case.ctx, TickReport())
    armed = timing_out(case.tracker, monkeypatch)
    armed.append(True)

    output = owner_process(case, monkeypatch)

    assert len(order_publications.pending(case.store)) == 2, statuses(case)
    lines = why_lines(output)
    assert len(lines) == 1, f"merge-owner.log must say once why publications were not applied:\n{output}"
    assert re.search(r"timed out|github", lines[0], re.I), lines[0]


def test_pass_skipped_during_github_pause_logs_one_line_why(case, monkeypatch):
    two_pending(case)
    order_publications.check_pending(case.ctx, TickReport())
    case.store.pause_github(datetime.now(UTC) + timedelta(minutes=30), "gh timed out after 30 seconds")

    output = owner_process(case, monkeypatch)

    assert len(order_publications.pending(case.store)) == 2, statuses(case)
    lines = why_lines(output)
    assert len(lines) == 1, f"merge-owner.log must say once why publications were not applied:\n{output}"
    assert re.search(r"github|paus|wait|timed out", lines[0], re.I), lines[0]


def test_pass_with_uncertified_publications_logs_one_line_why(case, monkeypatch):
    two_pending(case)  # The off-owner checker has not certified them on the current target.

    output = owner_process(case, monkeypatch)

    assert len(order_publications.pending(case.store)) == 2, statuses(case)
    lines = why_lines(output)
    assert len(lines) == 1, f"merge-owner.log must say once why publications were not applied:\n{output}"
    assert re.search(r"check|certif|red|wait", lines[0], re.I), lines[0]

    # Once certified, the next pass applies both and has nothing to explain.
    order_publications.check_pending(case.ctx, TickReport())
    output = owner_process(case, monkeypatch)

    assert not order_publications.pending(case.store), statuses(case)
    assert not [line for line in why_lines(output)
                if re.search(r"not applied|pending|why|stopp|skip", line, re.I)], output
