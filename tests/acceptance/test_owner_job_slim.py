"""Acceptance test for owner-job-slim. Locked by Foragent; do not edit.

A queued PR's merge-owner job file kept a full copy of the acceptance reports
(one file was 1.2 MB, 99% that copy), the way merge-queue.jsonl did before it
was slimmed. The owner's durable state must not grow with the size of the CI
reports, while a queued PR still merges from disk after a restart without a
new CI round, and a PR refreshed onto a moved main still merges once on its
own new round.

Real runner, public gate tick, merge owner, Git, state and small-repository
pytest; only GitHub, models and the clock are fake (the merge-owner prototype
world).
"""

from pathlib import Path

from acceptance.test_merge_path_real_settings import world  # noqa: F401
from test_merge_owner_proto import proto  # noqa: F401
import test_merge_owner_proto as support

PAD = 400_000  # Real reports are about 1.2 MB; any size must leave the state small.
LIMIT = 100_000


def large_reports(case, monkeypatch):
    """GitHub's run metadata is large and identical on every read of the same head."""
    reports = case.tracker.pr_acceptance_reports

    def measured(head):
        items = reports(head)
        for item in items:
            item["run"]["display_title"] = "x" * PAD
        return items

    monkeypatch.setattr(case.tracker, "pr_acceptance_reports", measured)


def state_files(case):
    root = Path(case.store.root)
    return {path: path.stat().st_size for path in root.rglob("*")
            if path.is_file() and path.suffix in {".json", ".jsonl"} and "logs" not in path.parts}


def assert_small(case):
    big = {str(path.relative_to(case.store.root)): size
           for path, size in state_files(case).items() if size > LIMIT}
    assert not big, f"State files grew with the acceptance reports: {big}"


def test_queued_job_does_not_keep_the_reports(proto, monkeypatch):
    """Waiting and after a restart: small state, and one merge of the measured head."""
    case = proto
    large_reports(case, monkeypatch)
    support.queue_workers(case, [5])
    pr = support.pr_for(case, 5).number
    queued = case.tracker.pr_head_sha(pr)
    rounds = len(case.ci.by_head)
    assert_small(case)

    # Every gate below reads the queue from disk with a new store and context.
    support.drain(case, count=1)
    assert case.tracker.merged_prs == [pr], "The queued PR merges once"
    assert case.merges[-1]["head"] == queued, "It merges the head its CI measured"
    assert len(case.ci.by_head) == rounds, "The stored evidence is enough; no new CI round"
    assert_small(case)


def test_a_refreshed_job_stays_small_and_merges_once(proto, monkeypatch):
    """Edge: main moves under a waiting PR; its refreshed evidence is stored small too."""
    case = proto
    large_reports(case, monkeypatch)
    support.queue_workers(case, [5, 1])
    first, second = (support.pr_for(case, n).number for n in (5, 1))
    queued = case.tracker.pr_head_sha(second)
    rounds = len(case.ci.by_head)

    support.drain(case, count=2)
    assert case.tracker.merged_prs == [first, second], "Each PR merges once, in join order"
    assert case.merges[-1]["head"] != queued, "The second PR merges only after its refresh"
    assert case.merges[-1]["head"] in case.ci.by_head and len(case.ci.by_head) == rounds + 1, (
        "One CI round for the refreshed head, none more")
    assert_small(case)
