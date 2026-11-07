"""Acceptance (neutral-during-train, statements 1-3). Locked by Foragent; do not edit.

Today the merge owner refuses every queued write to main while a train exists,
except D111 role-memory writes, so a publication must fall into a gap between
~55-minute trains. After this issue the owner lands a queued write during a
running train when the write is neutral for the train: its changed paths are
disjoint from the train members' changed files and from CI inputs (workflows,
conftest, pyproject). The train then lands onto the moved target without its CI
running again. A write that is not neutral waits for the train and lands before
the next train is built.

Real gate tick, merge owner, merge train, main-write queue, Git and state of the
locked merge-path world (tests/test_merge_owner_batch.py `batched`); only
GitHub, models and the clock are fake. A write is a request in the merge
owner's main-write queue (foragent.main_write_requests.submit), the queue
through which orders, notes and memory reach main.
"""

import base64
from pathlib import Path
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import batched, green, groups, queue_workers, refs  # noqa: E402, F401
from foragent import main_write_requests  # noqa: E402

MEMBERS = [5, 1]
SPEC = "docs/specs/2026-11-07-neutral-order.md"
NEW_TEST = "tests/acceptance/test_neutral_order_new.py"
SHARED_DOC = "docs/member-guide.md"   # changed by train member 5
OTHER_DOC = "docs/other-guide.md"     # changed by no member


# ---------------------------------------------------------------- helpers

def blob(text):
    return {"mode": "100644", "bytes": base64.b64encode(text.encode()).decode("ascii")}


def main_text(case, path):
    try:
        return git_command(case.history.origin, "show", f"main:{path}")
    except Exception:
        return None


def main_tip(case):
    return git_command(case.history.origin, "rev-parse", "refs/heads/main")


def submit_write(case, title, changes):
    """Queue one write: {path: new text}. `before` is main's current content."""
    files = {}
    for path, text in changes.items():
        current = main_text(case, path)
        files[path] = {"before": None if current is None else blob(current + "\n"), "after": blob(text)}
    return main_write_requests.submit(case.store, "main", title, "pm", {
        "kind": "files", "source": str(case.root), "base": main_tip(case), "files": files})


def write_status(case, row):
    return next(r for r in main_write_requests.requests(case.store) if r["id"] == row["id"])["status"]


def members_landed(case):
    return {n for n, _ in case.store.owner_merge_facts()}


def seed_main(case, files):
    case.history.update(files, message="Seed main")
    git_command(case.root, "fetch", "origin")


def member_also_edits(case, monkeypatch, n, path, text):
    """Train member `n` also changes `path` (a non-CI, non-memory file)."""
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def edit(**kwargs):
        result = implement(**kwargs)
        if case.worker.number == n:
            target = kwargs["worktree"] / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        return result

    monkeypatch.setattr(model, "run", edit)


def waiting_train(case, members=MEMBERS):
    """A train is built and published; its CI is pending."""
    case.pending_batch = True
    queue_workers(case, members)
    support.gate(case)
    assert len(groups(case)) == 1 and refs(case), "the train was not built"
    assert not members_landed(case), "the train must still wait for its CI"
    pr = groups(case)[0]
    return pr, case.tracker.pr_head_sha(pr.number)


def published_tip_contains(case, pr, commit):
    """The train's published tip (its PR head on the remote) has `commit` in its history."""
    tip = case.tracker.pr_head_sha(pr.number)
    try:
        git_command(case.history.origin, "merge-base", "--is-ancestor", commit, tip)
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- statement 1

def test_publication_lands_during_pending_train_and_train_lands_without_new_ci(batched):
    case = batched
    pr, tip = waiting_train(case)
    rounds = len(case.ci.by_head)
    before = main_tip(case)

    spec = "# Neutral order\nThe owner lands neutral writes during a train.\n"
    test = "def test_neutral_order():\n    assert False\n"
    row = submit_write(case, "Publish order neutral-order", {SPEC: spec, NEW_TEST: test})
    support.gate(case)  # One owner pass, the train's CI still pending.

    assert write_status(case, row) == "done", (
        "A publication of docs/specs and a new acceptance test, disjoint from the train members and "
        "from CI inputs, lands on main within one owner pass while the train's CI is pending")
    assert main_text(case, SPEC) == spec.rstrip("\n")
    assert main_text(case, NEW_TEST) == test.rstrip("\n")
    assert git_command(case.history.origin, "rev-list", "--count", f"{before}..main") == "1", (
        "The publication moves main by one commit")
    assert not members_landed(case), "The train is still waiting for its CI"
    assert refs(case) and len(groups(case)) == 1, "The neutral write does not retire or rebuild the train"

    green(case)
    support.gate(case)
    support.gate(case)

    assert members_landed(case) == set(MEMBERS), "The train lands onto the moved target"
    assert len(groups(case)) == 1, "The train is not rebuilt for the moved target"
    assert case.tracker.pr_head_sha(pr.number) == tip, "The train's tip is not refreshed"
    assert len(case.ci.by_head) == rounds, "The train lands without its CI running again"
    for n in MEMBERS:
        assert main_text(case, f"src/member_{n}.py") == f"VALUE = {n}"
    assert main_text(case, SPEC) == spec.rstrip("\n"), "The landed train keeps the publication on main"
    assert main_text(case, NEW_TEST) == test.rstrip("\n")


# ---------------------------------------------------------------- statement 2

def test_write_to_a_member_file_waits_for_the_train_and_lands_after_it(batched, monkeypatch):
    case = batched
    seed_main(case, {SHARED_DOC: "# Member guide\n\nintro\n\n\n\n\nend\n",
                     OTHER_DOC: "# Other guide\n"})
    member_also_edits(case, monkeypatch, 5, SHARED_DOC, "# Member guide\n\nintro by member 5\n\n\n\n\nend\n")
    pr, _tip = waiting_train(case)

    neutral = submit_write(case, "Other guide", {OTHER_DOC: "# Other guide\nUpdated.\n"})
    shared = submit_write(case, "Member guide ending",
                          {SHARED_DOC: "# Member guide\n\nintro\n\n\n\n\nend, revised\n"})
    support.gate(case)

    assert write_status(case, neutral) == "done", (
        "A write disjoint from the train lands while the train's CI is pending")
    assert write_status(case, shared) != "done", "A write to a file a train member changes waits for the train"
    assert "revised" not in (main_text(case, SHARED_DOC) or "")
    assert not members_landed(case)

    green(case)
    support.gate(case)
    support.gate(case)

    assert members_landed(case) == set(MEMBERS), "The train lands with the CI it measured"
    assert write_status(case, shared) == "done", "The waiting write lands after the train"
    guide = main_text(case, SHARED_DOC)
    assert "intro by member 5" in guide and "end, revised" in guide, "Both the train's and the write's edits stay"
    landed = next(r for r in main_write_requests.requests(case.store) if r["id"] == shared["id"])["landed"]
    merged = case.store.owner_merge_facts()
    assert all(case.git.is_ancestor(fact["head"], landed) for fact in merged.values()), (
        "The waiting write lands after the train, on top of it")


def test_waiting_member_write_lands_before_the_next_train_is_built(batched, monkeypatch):
    """The next train's base contains the write that waited for the previous train."""
    case = batched
    seed_main(case, {SHARED_DOC: "# Member guide\n\nintro\n\n\n\n\nend\n"})
    member_also_edits(case, monkeypatch, 5, SHARED_DOC, "# Member guide\n\nintro by member 5\n\n\n\n\nend\n")
    first, _tip = waiting_train(case)
    spec = submit_write(case, "Spec during train", {SPEC: "# Spec\n"})
    shared = submit_write(case, "Member guide ending",
                          {SHARED_DOC: "# Member guide\n\nintro\n\n\n\n\nend, revised\n"})
    support.gate(case)
    assert write_status(case, spec) == "done", "The neutral write lands during the train"
    assert write_status(case, shared) != "done", "The member-file write waits"

    case.pending_batch = True
    queue_workers(case, [4, 2])
    for pr in groups(case):
        case.tracker.set_pr_checks(pr.number, "pass")
    case.pending_batch = False
    for _ in range(4):
        support.gate(case)
        case.pending_batch = True
        if len(groups(case)) > 1:
            break
    assert members_landed(case) >= set(MEMBERS)
    later = [p for p in groups(case) if p.number != first.number]
    assert later, "A next train is built for the work queued during the first"
    landed = next(r for r in main_write_requests.requests(case.store) if r["id"] == shared["id"]).get("landed")
    assert landed, "The waiting write landed"
    assert published_tip_contains(case, later[0], landed), (
        "The waiting write lands before the next train is built: the next train's published tip contains it")
    tip = case.tracker.pr_head_sha(later[0].number)
    assert "end, revised" in git_command(case.history.origin, "show", f"{tip}:{SHARED_DOC}"), (
        "The next train is built on a target that has the waiting write")


# ---------------------------------------------------------------- statement 3

@pytest.mark.parametrize("ci_input", [".github/workflows/ci.yml", "tests/conftest.py", "pyproject.toml"])
def test_write_to_a_ci_input_waits_for_the_train(batched, ci_input):
    case = batched
    if main_text(case, ci_input) is None:
        seed_main(case, {ci_input: "# seed\n"})
    pr, _tip = waiting_train(case)
    rounds = len(case.ci.by_head)

    neutral = submit_write(case, "Spec during train", {SPEC: "# Spec\n"})
    current = main_text(case, ci_input)
    changed = current + "\n# changed during the train\n"
    ci_write = submit_write(case, f"Change {ci_input}", {ci_input: changed})
    support.gate(case)

    assert write_status(case, neutral) == "done", "A neutral write queued before it still lands during the train"
    assert write_status(case, ci_write) != "done", f"A write to the CI input {ci_input} waits for the train"
    assert main_text(case, ci_input) == current
    assert refs(case) and not members_landed(case), "The train is not disturbed"

    green(case)
    support.gate(case)
    support.gate(case)
    assert members_landed(case) == set(MEMBERS), "The train lands with the CI it measured"
    assert len(case.ci.by_head) == rounds
    assert write_status(case, ci_write) == "done", "After the train the CI-input write lands"
    assert "# changed during the train" in main_text(case, ci_input)
