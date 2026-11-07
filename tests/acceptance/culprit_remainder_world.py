"""Merge-owner train world for the culprit-remainder acceptance tests.

Real gate tick, merge owner, merge train, Git and state of the merge-path world
(tests/helpers/merge_world.py through tests/test_merge_owner_proto.py). Only
GitHub, models, the process table and the clock are fake. This is a copy of the
`batched` fixture of tests/test_merge_owner_batch.py, kept here so that a split
of that unit file does not change these acceptance tests.
"""

from dataclasses import replace
from pathlib import Path
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
import test_merge_owner_proto as support  # noqa: E402
from foragent import merge_owner, merge_owner_batch as batch  # noqa: E402
from foragent.git import GitError  # noqa: E402
from foragent.tracker import FakeTracker, TrackerError  # noqa: E402


@pytest.fixture
def trains(proto, monkeypatch):
    case = proto
    case.config = replace(case.config, merge_batch=True, merge_batch_size=4)
    case.ctx.config = case.config
    case.ctx.reads = merge_owner.Snapshot(case.tracker)
    case.pending_batch = case.github_lag = case.merge_tip_metadata = False
    case.batch_pushes, case.target_pushes, case.target_attempts = [], [], []
    case.target_failure = case.after_target_push = None
    for key in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
        monkeypatch.setenv(key, case.clock[0].isoformat())
    transport = case.git._run

    def git_transport(argv):
        root, args = case.root, list(argv)
        while args[:1] == ["-C"]:
            root, args = Path(args[1]), args[2:]
        if args[:1] == ["push"]:
            source, destination = args[-1].split(":", 1) if ":" in args[-1] else (args[-1], args[-1])
            lease = next((a.split("=", 1)[1] for a in args if a.startswith("--force-with-lease=")), None)
            if lease is not None:
                ref, expected = lease.split(":", 1)
                actual = git_command(case.history.origin, "for-each-ref", "--format=%(objectname)", ref)
                if actual != expected:
                    raise GitError("lease rejected")
            if destination in {"main", "refs/heads/main"}:
                # No foragent lock is required here: queue-only-main forbids holding
                # one across a push, and this issue does not change lock rules.
                assert lease is not None and ref in {"main", "refs/heads/main"}
                case.target_attempts.append((source, destination, expected))
                if case.target_failure:
                    raise GitError(case.target_failure)
                head = git_command(root, "rev-parse", source)
                git_command(root, "merge-base", "--is-ancestor", expected, head)
                git_command(case.history.origin, "fetch", str(root), source)
                git_command(case.history.origin, "update-ref", "refs/heads/main", head, expected)
                case.history.tip = head
                case.target_pushes.append((head, destination, expected))
                if case.after_target_push:
                    case.after_target_push()
                return ""
            if source and batch.PREFIX in destination:
                case.batch_pushes.append((source, destination))
        return transport(argv)

    monkeypatch.setattr(case.git, "_run", git_transport)
    create, get = case.tracker.create_pr, case.tracker.get_pr

    def created(**kwargs):
        if kwargs["head"].startswith(batch.PREFIX) and any(
                pr.head == kwargs["head"] and pr.base == kwargs["base"]
                and case.tracker.get_issue(pr.number).state == "open"
                for pr in case.tracker.created_prs):
            raise TrackerError("A pull request already exists for this head/base")
        pr = create(**kwargs)
        if kwargs["head"].startswith(batch.PREFIX) and case.pending_batch:
            case.tracker.set_pr_checks(pr, "pending")
        return pr

    def github_metadata(pr):
        row = get(pr)
        eligible = row.head.startswith("foragent/issue-") or case.merge_tip_metadata and row.head.startswith(batch.PREFIX)
        if eligible and not case.github_lag and not row.merged_at and row.state == "open":
            if case.git.at(case.history.origin).is_ancestor(row.head_sha, f"refs/heads/{row.base}"):
                FakeTracker.merge_pr(case.tracker, pr, expected_head_sha=row.head_sha, delete_branch=False)
                row = get(pr)
        return row

    monkeypatch.setattr(case.tracker, "create_pr", created)
    monkeypatch.setattr(case.tracker, "get_pr", github_metadata)
    reports = case.tracker.pr_acceptance_reports

    def measured(head):
        row = case.ci.by_head[head]
        return [] if row.branch.startswith(batch.PREFIX) and case.pending_batch else reports(head)

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured)
    return case


CULPRIT = 1
OUTSIDER = 2  # approved and queued while the train runs; not a train member


def new_train_members(case, before):
    return [composed(case, pr) for pr in groups(case)[before:]]


def queue_workers(case, numbers):
    support.queue_workers(case, numbers)
    if case.pending_batch:
        for n in numbers:
            case.tracker.set_pr_checks(support.pr_for(case, n).number, "pending")


def set_own_ci(case, n, state):
    """The member's own PR CI; its PR head is based on the current main."""
    case.tracker.set_pr_checks(support.pr_for(case, n).number, state)


def groups(case):
    return [pr for pr in case.tracker.created_prs if pr.head.startswith(batch.PREFIX)]


def worker_prs(case):
    """Issue number -> its worker PR (head foragent/issue-<n>)."""
    return {int(pr.head.rsplit("-", 1)[1]): pr for pr in case.tracker.created_prs
            if pr.head.startswith("foragent/issue-")}


def _contains(case, tip, head):
    try:
        git_command(case.history.origin, "merge-base", "--is-ancestor", head, tip)
        return True
    except Exception:
        return False


def composed(case, pr):
    """Train membership as GitHub shows it: the worker PRs whose heads the train's head contains."""
    tip = case.tracker.pr_head_sha(pr.number)
    git_command(case.history.origin, "cat-file", "-e", f"{tip}^{{commit}}")  # the train head is on origin
    return sorted(n for n, w in worker_prs(case).items()
                  if _contains(case, tip, case.tracker.pr_head_sha(w.number)))


def closed_unmerged(case, pr_number):
    row = case.tracker.get_pr(pr_number)
    return row.state == "closed" and not row.merged_at


def landed(case):
    """Issue number -> how many times the owner recorded it as merged."""
    counts = {}
    for n, _ in case.store.owner_merge_facts():
        counts[n] = counts.get(n, 0) + 1
    return counts


def on_main(case, n):
    try:
        return git_command(case.history.origin, "show", f"main:src/member_{n}.py") == f"VALUE = {n}"
    except Exception:
        return False


def build_train(case, members):
    """Publish one train of `members`; its tip CI stays pending."""
    case.pending_batch = True
    queue_workers(case, members)
    support.gate(case)
    trains = groups(case)
    assert len(trains) == 1, f"one train is built, got {[(p.number, p.head) for p in trains]}"
    assert sorted(composed(case, trains[0])) == sorted(members)
    assert not landed(case), "the train waits for its CI"
    return trains[0]


def culprit_at_final_check(case, monkeypatch, train, culprit):
    """The train's tip CI passes; the owner's final check then names `culprit`.

    Runs the one owner pass that retires the train. A remainder that is already
    verified may land in this same pass (reviewer hold d); nothing here forbids
    it. Every train or PR CI that starts after the final check stays pending
    until the test changes it, so a landing that needs a new CI run cannot
    happen. Returns how many trains existed before any new one.

    The failure comes only from the fake GitHub check transport (review hold d,
    attempt 15): the culprit's own PR CI turns red on the first read of it that
    follows a read of the train tip CI as passing, that is, at the owner's
    final recheck. It does not depend on which lock, if any, is held then.
    """
    before = len(groups(case))
    tip_head = case.tracker.pr_head_sha(train.number)
    culprit_pr = support.pr_for(case, culprit).number
    checks, create, reports = case.tracker.pr_checks, case.tracker.create_pr, case.tracker.pr_acceptance_reports
    seen = {"tip_pass": False, "flipped": False}

    def github_checks(pr):
        status = checks(pr)
        if pr == train.number and status == "pass":
            seen["tip_pass"] = True
        if pr == culprit_pr and seen["tip_pass"] and not seen["flipped"]:
            seen["flipped"] = True
            set_own_ci(case, culprit, "fail")  # the culprit's PR CI is red at the final recheck
            status = checks(pr)
        return status

    def new_train_pending(**kwargs):
        pr = create(**kwargs)
        if kwargs["head"].startswith(batch.PREFIX):
            case.tracker.set_pr_checks(pr, "pending")  # a new train CI does not finish in this pass
        return pr

    def only_old_tip_measured(head):
        row = case.ci.by_head[head]
        return [] if row.branch.startswith(batch.PREFIX) and head != tip_head else reports(head)

    case.pending_batch = False
    case.tracker.set_pr_checks(train.number, "pass")
    with monkeypatch.context() as patch:
        patch.setattr(case.tracker, "pr_checks", github_checks)
        patch.setattr(case.tracker, "create_pr", new_train_pending)
        patch.setattr(case.tracker, "pr_acceptance_reports", only_old_tip_measured)
        support.gate(case)
    assert seen["flipped"], "the owner's final check reads the culprit's own PR CI after the train tip passed"
    case.pending_batch = True
    assert closed_unmerged(case, train.number), "the final check retires the train: its PR closes unmerged"
    assert closed_unmerged(case, culprit_pr), "the named culprit's PR is returned (closed, not merged)"
    assert culprit not in landed(case) and not on_main(case, culprit), "the culprit does not land"
    others = [n for n in composed(case, train) if n != culprit]
    assert not [n for n in others if closed_unmerged(case, worker_prs(case)[n].number)], (
        "only the culprit is named: the other members' PRs are not returned")
    return before


def remaining_member_lands_without_new_train_ci(case, monkeypatch, outsider):
    """Statement 1: train {#5, culprit}; #5's own PR CI passed on the current main."""
    train = build_train(case, [5, CULPRIT])
    if outsider:
        queue_workers(case, [OUTSIDER])  # its own PR CI stays pending
    set_own_ci(case, 5, "pass")  # PR CI of #5 ran on the current main SHA and passed
    before = culprit_at_final_check(case, monkeypatch, train, CULPRIT)  # #5 may land in this pass

    for _ in range(2):
        if 5 in landed(case):
            break
        support.gate(case)

    assert landed(case).get(5) == 1, (
        "The only remaining member, whose PR CI passed on the current main, lands from the retiring pass onward, within two more owner passes "
        f"while every new train CI is still pending; landed: {landed(case)}, "
        f"new trains: {new_train_members(case, before)}")
    assert on_main(case, 5), "#5's change is on main"
    assert not any(5 in members for members in new_train_members(case, before)), (
        f"#5 is not put into a new train: {new_train_members(case, before)}")
    assert CULPRIT not in landed(case) and not on_main(case, CULPRIT), "the culprit does not land"
    assert OUTSIDER not in landed(case) and not on_main(case, OUTSIDER), (
        "a queued PR whose own CI is pending does not land with #5")

    support.gate(case)
    assert landed(case).get(5) == 1, f"#5 lands once: {landed(case)}"
    merged = [m for m in case.merges if m["pr"] == support.pr_for(case, 5).number]
    assert len(merged) + len(case.target_pushes) <= 1, (
        f"main moves once for #5: merges {merged}, pushes {case.target_pushes}")
