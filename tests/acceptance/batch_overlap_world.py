"""Shared helpers for batch-recheck-on-overlap-only acceptance tests.

Real gate tick, merge owner, merge train, Git (bare origin) and state of the
locked merge-path world (tests/helpers/merge_batch.py `batched`); only GitHub,
models and the clock are fake.

Train members 5 and 1 each change src/member_<n>.py. Member 5 also changes the
first line of src/shared.py. Main then moves with one ordinary code commit:
- a disjoint move changes src/neighbor.py, a file no member changes;
- an overlapping move changes the last line of src/shared.py, a file member 5
  changes (no textual conflict, so the train can be rebuilt on the new main).
Neither move is result-neutral (both are source files), so today's
neutral-move rule does not keep the train.
"""

from contextlib import contextmanager
from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import green, groups, queue_workers, refs  # noqa: E402
from foragent import merge_owner, merge_owner_batch as batch  # noqa: E402
from foragent.state import StateStore  # noqa: E402

MEMBERS = [5, 1]
SHARED = "src/shared.py"
NEIGHBOR = "src/neighbor.py"
PAD = "".join(f"# filler line {i}\n" for i in range(12))
SHARED_BASE = "VALUE = 0\n" + PAD + "TAIL = 'base'\n"
SHARED_MEMBER = "VALUE = 5\n" + PAD + "TAIL = 'base'\n"
LEDGER = "docs/decisions.md"
LEDGER_BASE = "# Decisions\n\n- first: seed\n" + PAD + "- last: seed\n"
LEDGER_MEMBER = "# Decisions\n\n- first: member 1\n" + PAD + "- last: seed\n"


def ledger_main(tag):
    """Main edits the last entry of the decision ledger (no member changes it unless asked)."""
    return {LEDGER: "# Decisions\n\n- first: seed\n" + PAD + f"- last: main {tag}\n"}


def shared_main(tag):
    """Main edits the last line of src/shared.py, which member 5 also changes."""
    return {SHARED: "VALUE = 0\n" + PAD + f"TAIL = 'main {tag}'\n"}


def neighbor_main(tag):
    return {NEIGHBOR: f"OTHER = 'main moved: {tag}'\n"}


def main_tip(case):
    return git_command(case.history.origin, "rev-parse", "refs/heads/main")


def main_text(case, path):
    return git_command(case.history.origin, "show", f"main:{path}")


def is_ancestor(case, older, newer):
    try:
        git_command(case.history.origin, "merge-base", "--is-ancestor", older, newer)
    except Exception:
        return False
    return True


def members_landed(case):
    return {n for n, _ in case.store.owner_merge_facts()}


def reasons(case):
    return [row["reason"] for row in batch.retirements(case.ctx)]


def train_tip(case, pr):
    """The SHA the train PR currently publishes (fake GitHub's PR head)."""
    return case.tracker.pr_head_sha(pr.number)


def text_at(case, sha, path):
    """File text at a published commit in the bare origin."""
    return git_command(case.history.origin, "show", f"{sha}:{path}")


def setup(case, monkeypatch, ledger_member=False):
    """Seed main with the shared, neighbor and ledger files; member 5 edits the shared file.

    With ledger_member, member 1 also edits the first entry of the decision ledger.
    """
    case.history.update(
        {SHARED: SHARED_BASE, NEIGHBOR: "OTHER = 'base'\n", LEDGER: LEDGER_BASE}, message="Seed main")
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "merge", "--ff-only", "origin/main")
    case.base = case.history.tip
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def edit(**kwargs):
        result = implement(**kwargs)
        if case.worker.number == 5:
            (kwargs["worktree"] / SHARED).write_text(SHARED_MEMBER)
        if ledger_member and case.worker.number == 1:
            (kwargs["worktree"] / LEDGER).write_text(LEDGER_MEMBER)
        return result

    monkeypatch.setattr(model, "run", edit)


def waiting_train(case, monkeypatch, members=MEMBERS, ledger_member=False):
    """A train of `members` is built and published; its tip CI is pending."""
    setup(case, monkeypatch, ledger_member)
    case.pending_batch = True
    queue_workers(case, members)
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "the train was not built"
    assert not members_landed(case), "the train must still wait for its CI"
    pr = groups(case)[0]
    return pr, case.tracker.pr_head_sha(pr.number)


def move_main(case, files, message="Main moves under the train"):
    case.history.update(files, message=message)
    return case.history.tip


def move_inside_final_checks(case, monkeypatch, files):
    """Move main once, after the unlocked tip checks, just before the locked final recheck."""
    lock = StateStore.merge_lock
    moved = []

    @contextmanager
    def move(store):
        with lock(store):
            if not moved:
                moved.append(move_main(case, files, message="Main moves during final checks"))
            yield

    monkeypatch.setattr(StateStore, "merge_lock", move)
    return moved


def record_tip_ci_reads(case, monkeypatch, pr):
    """Record every fake-GitHub read of the train PR's CI as the number of main pushes tried so far."""
    reads = []
    checks = case.tracker.pr_checks

    def read(number, *args, **kwargs):
        if number == pr.number:
            reads.append(len(case.target_attempts))
        return checks(number, *args, **kwargs)

    monkeypatch.setattr(case.tracker, "pr_checks", read)
    return reads


def tip_ci_turns_red_inside_final_checks(case, monkeypatch, pr):
    """GitHub reports the tip CI red once the owner enters its locked final recheck."""
    lock = StateStore.merge_lock
    flipped = []

    @contextmanager
    def red(store):
        with lock(store):
            if not flipped:
                case.tracker.set_pr_checks(pr.number, "fail")
                flipped.append(True)
            yield

    monkeypatch.setattr(StateStore, "merge_lock", red)
    return flipped


def check_lines(output, tip):
    """merge-owner.log lines for the owner's machine checks of `tip`."""
    return [line for line in output.splitlines()
            if line.startswith("merge owner checks ") and f" at {tip[:12]}" in line]


__all__ = [
    "LEDGER", "LEDGER_MEMBER", "ledger_main", "MEMBERS", "NEIGHBOR", "SHARED", "green", "groups", "is_ancestor", "main_text", "main_tip",
    "members_landed", "move_inside_final_checks", "move_main", "neighbor_main", "reasons", "refs",
    "shared_main", "support", "train_tip", "text_at", "record_tip_ci_reads", "tip_ci_turns_red_inside_final_checks",
    "check_lines", "waiting_train",
]
