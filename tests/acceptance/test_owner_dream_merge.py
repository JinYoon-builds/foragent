"""Acceptance test for owner-dream-merge. Locked by Foragent; do not edit.

With the merge owner on, `fa orch merge` refuses every PR that is not an
approved issue PR, so lane dream PRs no longer merge (six dream PRs open since
2026-10-29). skills/fa-orchestrator/references/dreams.md makes the orchestrator
merge a green dream PR itself. That must work with the owner on, and the
existing staleness refusal (a dream whose inputs moved on main) must stay.

Real orchestrator merge, public gate tick, merge owner, Git, state and
small-repository pytest; only GitHub, models and the clock are fake (the
merge-owner prototype world).
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from conftest import git_command
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support
from foragent import orch_dream
from foragent.orch import OrchError, merge
from foragent.orch_dream_publish import fingerprints

LANE = "orch"


def publish_dream(case, memory, content):
    """The lane's dream branch with one curated-memory edit, published through the CI transport."""
    base = git_command(case.history.origin, "rev-parse", "main")
    message = "Lane memory\n\nForagent-Acceptance-Excluded: []\n"
    git_command(case.root, "fetch", "origin")
    git_command(case.root, "fast-import", "--quiet", "--force", input=(
        f"commit refs/heads/dream-source\ncommitter Dream <dream@example.com> 1792483300 +0000\n"
        f"data {len(message)}\n{message}from {base}\n"
        f"M 100644 inline {memory}\ndata {len(content)}\n{content}\n\ndone\n"))
    case.git._call(["push", "origin", f"refs/heads/dream-source:refs/heads/foragent/dream-{LANE}"])
    pr = case.tracker.create_pr(head=f"foragent/dream-{LANE}", base="main",
                                title=f"Lane {LANE} memory", body="Only the lane's memory directory changes.")
    # The dream read no curated memory on the base (absent files) and wrote one.
    orch_dream.record_published(case.store, LANE, 1, case.clock[0], covered_until=case.clock[0], pr=pr,
                                inputs=fingerprints({}), outputs=fingerprints({"MEMORY.md": content.encode()}))
    return pr


def test_orchestrator_merges_a_dream_pr_with_the_owner_on(proto):
    """dreams.md: the orchestrator merges a green lane dream PR; a stale one is still refused."""
    case = proto
    case.config = replace(case.config, lanes={LANE: {"paths": ["src/orch/**"]}})
    case.ctx.config = case.config
    memory = orch_dream.memory_path(case.config, LANE)
    pr = publish_dream(case, memory, "- folded lesson\n")

    consumed = memory.rsplit("/", 1)[0] + "/MEMORY.consumed.ids"
    # Edge: a hand edit of the lane's consumed ids on main makes the dream stale.
    case.history.update({consumed: "hand-edit\n"}, message="Hand edit")
    case.clock[0] += timedelta(seconds=1)
    with pytest.raises(OrchError, match="stale"):
        merge(case.ctx, pr, approved_by="human")
    support.drain(case, count=1)
    assert not case.tracker.merged_prs, "A stale dream PR never merges"

    # The hand edit is reverted on main; the dream's inputs match again.
    case.history.update({consumed: None}, message="Revert hand edit")
    case.clock[0] += timedelta(seconds=1)
    merge(case.ctx, pr, approved_by="human")
    support.drain(case, count=1)

    assert case.tracker.merged_prs == [pr], "The dream PR merges, once"
    assert git_command(case.history.origin, "show", f"main:{memory}") == "- folded lesson"
    tree = git_command(case.history.origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert consumed not in tree, "The merge keeps main's own change (the reverted hand edit)"
    assert not case.launches
