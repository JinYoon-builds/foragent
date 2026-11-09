"""Shared helpers for the workflow-changing member acceptance tests (PM r1280).

They use the existing merge owner world: a fake tracker, local Git with a bare
origin, measured tip CI and the public scheduler tick. No network.
"""

from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

import test_merge_owner_proto as support  # noqa: E402
from foragent import ci_local, merge_owner, merge_owner_batch as batch  # noqa: E402
from helpers.merge_batch import groups  # noqa: E402

WORKFLOW = ".github/workflows/foragent-acceptance.yml"
CHANGED = "name: changed-acceptance\non: [pull_request]\njobs: {}"


def head(case, n):
    return case.tracker.pr_head_sha(support.pr_for(case, n).number)


def batches(case):
    """Members of every published batch tip, in publication order."""
    found = []
    for pr in groups(case):
        _, chain = batch._decode(case.git, pr.head, case.tracker.pr_head_sha(pr.number))
        found.append([n for n, *_ in chain])
    return found


def landed_batches(case):
    """Members of every batch whose tip reached main, in landing order."""
    tips = [sha for sha, *_ in case.target_pushes]
    found = []
    for pr in groups(case):
        tip = case.tracker.pr_head_sha(pr.number)
        if tip in tips:
            _, chain = batch._decode(case.git, pr.head, tip)
            found.append((tips.index(tip), [n for n, *_ in chain]))
    return [numbers for _, numbers in sorted(found)]


def merged(case, n):
    """True once the merge owner recorded #n as merged."""
    return any(key[0] == n for key in case.store.owner_merge_facts())


def merge_facts(case, n):
    return [fact for key, fact in case.store.owner_merge_facts().items() if key[0] == n]


def record_checks(monkeypatch):
    """Keep the real local merge-owner checks and note which members ran them."""
    fallback, measured = ci_local.fallback_comparison, []

    def local(attempt, base_sha, **kwargs):
        measured.append(attempt.n)
        return fallback(attempt, base_sha, **kwargs)

    monkeypatch.setattr(ci_local, "fallback_comparison", local)
    return measured


def waiting(case):
    return sorted(n for n, _ in merge_owner.admissions(case.store))
