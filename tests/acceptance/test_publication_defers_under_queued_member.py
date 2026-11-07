"""Acceptance test for publication-defers-under-queued-member.

Each merge-owner pass applies pending order publications before it merges members.
A publication whose files overlap a merge-queued member's changed paths must wait:
it is not applied in that pass, the member's train tip is built without it, and it
is applied in a following pass once the member merges or leaves the merge queue.
Publications over no queued member's paths apply as today; current tests guard that.

Real merge owner pass (``merge_owner.tick``), real off-owner checker
(``order_publications.check_pending``), real publication queue and merge queue file.
Fake Git remote, fake GitHub and measured test runs come from
tests/test_order_publications.py. Since #2161 an order writes no spec file, so the
project here keeps its acceptance tests under docs/specs/ (config.acceptance_dir): the
submitted publication then really adds a docs/specs file, and its preparation record is
rebuilt for that plan as a fresh preparation would. The member's PR reconciliation is replaced so the
member stays a waiting candidate, and the train builder (merge_batch on) records the
target it would build the member's tip on. The member's changed paths come from the
fake GitHub PR file list and from a fake `git diff` against its head. No network,
model or signal.
"""

from dataclasses import replace
from datetime import UTC, datetime
import json

import pytest

from test_order_publications import case  # noqa: F401  (fixture)
from test_order_publications import submit

SPEC = "docs/specs/test_publication.py"  # The publication's file: a new docs/specs file.
SPEC_COMMAND = f"uv run pytest {SPEC}"
STARTED = "2026-11-07T03:00:00+00:00"
MEMBER_HEAD = "member-head"
MEMBER_BRANCH = "foragent/issue-member"
OLD_SPECS = {"docs/specs/old-a.md": "# Old A\n", "docs/specs/old-b.md": "# Old B\n"}


def _status(case):
    from foragent import order_publications

    row, = [row for row in order_publications.requests(case.store) if row["plan"]["id"] == case.plan["id"]]
    return row["status"]


def _remote_files(case):
    return set(case.git.remote.snapshots.get(case.git.remote.head, {}))


def _pushes(case):
    return [event for event in case.events if event[0] == "push"]


def _publication_commits(case):
    """Remote commits whose tree first adds the order's spec file."""
    remote = case.git.remote
    return [sha for sha, files in remote.snapshots.items()
            if SPEC in files and SPEC not in remote.snapshots.get(remote.parents.get(sha), {})]


@pytest.fixture
def world(case, monkeypatch):  # noqa: F811
    """The publication case with one merge-queued member and a recorded train builder."""
    from foragent import merge_owner, merge_owner_batch, run_merge_turn as queue

    from foragent import order, order_prepare, order_prepare_cycle, order_prepare_setup

    case.config = replace(case.config, merge_batch=True, acceptance_dir="docs/specs/")
    case.ctx.config = case.config
    item = case.plan["issues"][0]
    item.update(acceptance_files={SPEC: "def test_new(): pass\n"}, done_when=[SPEC_COMMAND])
    files = order.validate_plan(case.plan, case.root, case.config)
    assert set(files) == {SPEC}, "The submitted publication adds exactly one docs/specs file"
    path = order_prepare._record_path(case.store, case.plan)
    record = order_prepare._read(path, case.plan)
    record["issues"]["new"].update(
        written=dict(item["acceptance_files"]), done_when=[SPEC_COMMAND],
        cycle=order_prepare_cycle.identity(case.plan, item, case.config, case.root, files),
        published=order_prepare_setup.published_digest(case.plan, case.root, case.config, files))
    order_prepare._write(path, record)
    case.ctx.now = lambda: datetime(2026, 11, 7, 4, 0, tzinfo=UTC)
    remote = case.git.remote
    remote.snapshots[remote.head] = {**remote.snapshots.get(remote.head, {}), **OLD_SPECS}

    def queue_member(changes, *, deleted=()):
        """Admit one member whose PR changes ``changes`` and deletes ``deleted``."""
        issue = case.tracker.create_issue("Queued member", "## Done when\n```sh\nuv run pytest\n```\n",
                                          ["lane:main"])
        pr = case.tracker.create_pr(head=MEMBER_BRANCH, base="main", title="Queued member", body="")
        paths = sorted([*changes, *deleted])
        case.tracker.set_pr_files(pr, paths)
        case.tracker.set_pr_head_sha(pr, MEMBER_HEAD)
        base = remote.head
        tree = {name: data for name, data in remote.snapshots.get(base, {}).items() if name not in deleted}
        remote.snapshots[MEMBER_HEAD] = {**tree, **changes}
        remote.parents[MEMBER_HEAD] = base
        case.member = {"n": issue, "pr": pr, "paths": paths, "base": base, "deleted": set(deleted)}
        place = queue.Seat(issue, STARTED, "main")
        job = {"pr": pr, "branch": MEMBER_BRANCH, "target": "main", "head": MEMBER_HEAD, "base": base,
               "approval": {}, "ready_at": STARTED, "authorization": None, "phase": "waiting"}
        case.store._write_json(case.store.root / "merge-owner" / f"{issue}.json",
                               {"started_at": STARTED, **job, "comparison": None})
        queue._append(case.store, queue._row(case.ctx, place, "ready", head_sha=MEMBER_HEAD,
                                             merge_owner=True, publication=job))
        case.place = place
        return place

    # Git answers the member's changed paths from its base, as `git diff --name-only` does.
    call = type(case.git)._call

    def git_call(self, args):
        if args and args[0] in {"diff", "diff-tree"} and MEMBER_HEAD in args and getattr(case, "member", None):
            separator = "\0" if "-z" in args else "\n"
            return separator.join(case.member["paths"]) + (separator if case.member["paths"] else "")
        return call(self, args)

    monkeypatch.setattr(type(case.git), "_call", git_call)

    # The member stays queued and waiting; the owner treats it as this pass's candidate.
    def reconcile(ctx, place, job):
        return ctx.tracker.get_pr(job["pr"])

    monkeypatch.setattr(merge_owner, "_reconcile", reconcile)

    # The train builder records the target its member tip would be built on.
    case.trains = []

    def prepare(ctx, report, candidates):
        head = remote.head
        case.trains.append({"base": head, "files": set(remote.snapshots.get(head, {})),
                            "members": [place.n for place, _job, _pr in candidates],
                            "status": _status(case)})
        return False

    monkeypatch.setattr(merge_owner_batch, "prepare", prepare)
    case.queue_member = queue_member
    return case


def submit_overlapping(case):
    """Submit the publication and check it is pending with its docs/specs file and red evidence."""
    from foragent import order_publications

    submit(case)
    row, = [row for row in order_publications.requests(case.store) if row["plan"]["id"] == case.plan["id"]]
    assert set(row["files"]) == {SPEC} and row["prepared"]["new"]["done_when"] == [SPEC_COMMAND], (
        "The pending publication adds the docs/specs file and keeps its preparation evidence")
    assert row["status"] == "pending"
    return row


def owner_pass(case):
    """What the gate does each tick: the off-owner check, then one merge-owner pass."""
    from foragent import merge_owner, order_publications
    from foragent.scheduler import TickReport

    report = TickReport()
    order_publications.check_pending(case.ctx, report)
    merge_owner.tick(case.ctx, report)
    return report


def member_merges(case):
    """The queued member's PR lands on main and the member leaves the merge queue."""
    from foragent import run_merge_turn as queue

    remote = case.git.remote
    merged = "main-merged"
    remote.snapshots[merged] = dict(remote.snapshots[MEMBER_HEAD])
    remote.parents[merged] = remote.head
    remote.parents[MEMBER_HEAD] = remote.head
    remote.head = merged
    case.tracker.merge_pr(case.member["pr"], expected_head_sha=MEMBER_HEAD)
    path = case.store.root / "merge-owner" / f"{case.member['n']}.json"
    job = json.loads(path.read_text())
    job["phase"] = "merged"
    case.store._write_json(path, job)
    queue.withdraw(case.ctx, case.place, reason="merged")


def member_leaves(case):
    """The queued member leaves the merge queue without merging."""
    from foragent import run_merge_turn as queue

    queue.withdraw(case.ctx, case.place, reason="repair")


def queued(case):
    from foragent import merge_owner

    return [key[0] for key in merge_owner.admissions(case.store)]


# Scenario check (passes on main) --------------------------------------------------------

def test_without_a_queued_member_the_docs_specs_publication_applies_in_the_first_pass(world):
    """The scenario is real: today the same publication lands its docs/specs file at once."""
    case = world
    submit_overlapping(case)
    owner_pass(case)
    assert _status(case) == "published" and SPEC in _remote_files(case)
    assert len(_pushes(case)) == 1 and len(_publication_commits(case)) == 1


# Statement 1 ---------------------------------------------------------------------------

@pytest.mark.parametrize("touches_same_file", [False, True], ids=["deletes-docs-specs", "also-edits-the-file"])
def test_a_publication_over_a_queued_members_docs_specs_waits_and_the_tip_is_built_without_it(world, touches_same_file):
    case = world
    case.queue_member({SPEC: "def test_member(): pass\n"} if touches_same_file else {}, deleted=OLD_SPECS)
    submit_overlapping(case)
    before = case.git.remote.head
    owner_pass(case)
    assert queued(case) == [case.member["n"]], "The member is still merge-queued after the pass"
    assert case.trains, "The owner pass reached its train builder"
    train = case.trains[-1]
    assert train["members"] == [case.member["n"]], "The pass builds the queued member's train"
    assert SPEC not in train["files"], (
        "The member's train tip must be built without the publication that adds a docs/specs file "
        f"under its changed paths, but the train base {train['base']} already holds {SPEC}")
    assert _status(case) == "pending", (
        f"The overlapping publication is not applied in this pass and stays pending, not {_status(case)!r}")
    assert case.git.remote.head == before and SPEC not in _remote_files(case), (
        "No publication commit lands on main under the queued member's paths")
    assert not _pushes(case), "The deferred publication pushed nothing"


def test_the_publication_keeps_waiting_on_every_pass_while_the_member_stays_queued(world):
    case = world
    case.queue_member({}, deleted=OLD_SPECS)
    submit_overlapping(case)
    for _ in range(3):
        owner_pass(case)
    assert _status(case) == "pending", "While the member stays queued the publication keeps waiting"
    assert SPEC not in _remote_files(case) and not _pushes(case)
    assert all(SPEC not in train["files"] for train in case.trains), (
        "Every train tip of the queued member is built without the publication")


# Statement 2 ---------------------------------------------------------------------------

@pytest.mark.parametrize("leaving", [member_merges, member_leaves], ids=["merges", "leaves-queue"])
def test_the_deferred_publication_is_applied_after_the_member_merges_or_leaves(world, leaving):
    case = world
    case.queue_member({}, deleted=OLD_SPECS)
    submit_overlapping(case)
    owner_pass(case)
    assert _status(case) == "pending", "Deferred while the member is queued"
    leaving(case)
    assert queued(case) == [], "The member is no longer merge-queued"
    owner_pass(case)
    assert _status(case) == "published", (
        f"After the member {leaving.__name__.split('_')[1]} the deferred publication is applied "
        f"in a following owner pass, not left {_status(case)!r}")
    assert SPEC in _remote_files(case), "The publication's docs/specs file is on main"
    if leaving is member_merges:
        assert case.git.is_ancestor("main-merged", case.git.remote.head), (
            "The publication lands on top of the merged member, not in place of it")
        assert not set(OLD_SPECS) & _remote_files(case), "The member's deletion stays on main"
    assert len(_pushes(case)) == 1, "The deferred publication is pushed exactly once"
    owner_pass(case)
    assert len(_pushes(case)) == 1 and len(_publication_commits(case)) == 1, (
        "A later pass does not apply the publication again")

