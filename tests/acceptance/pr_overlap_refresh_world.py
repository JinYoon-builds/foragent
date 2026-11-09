"""Shared world for pr-refresh-on-overlap-only acceptance tests.

The merge-owner world of tests/test_merge_owner_proto.py (real runner, scheduler
tick, merge owner, Git with a bare origin and state; only GitHub, models, the
process launcher and the clock are fake). Issue 5 opens its PR, passes CI and is
admitted to the merge owner. Its PR changes two files:

- src/member_5.py (new), and
- src/shared.py, first line only, so main can edit the last line of the same
  file and the base still merges in without a conflict.

Main then moves under the open PR, and the next owner tick decides whether the
PR is refreshed onto the new base. Observations: pushes of the PR branch, CI
rounds GitHub would start, which PR head merged and what main contains, never
log wording.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
import test_merge_owner_proto as support  # noqa: E402
from foragent import merge_owner, run_merge_turn as queue  # noqa: E402
from foragent.git import GitRepo  # noqa: E402

N = 5
SHARED = "src/shared.py"
PAD = "".join(f"# filler line {i}\n" for i in range(12))
BASE_SHARED = "FIRST = 'base'\n" + PAD + "LAST = 'base'\n"
PR_SHARED = "FIRST = 'pr'\n" + PAD + "LAST = 'base'\n"


def main_shared(tag):
    """Main edits only the last line of src/shared.py, a file the PR changes."""
    return {SHARED: "FIRST = 'base'\n" + PAD + f"LAST = 'main {tag}'\n"}


def admitted_pr(case, monkeypatch):
    """Run issue 5 until its green PR waits in the merge owner; return its job,
    the list that records every later PR push, and the CI round count."""
    case.history.update({SHARED: BASE_SHARED})
    git_command(case.root, "fetch", "-q", "origin")
    git_command(case.root, "merge", "-q", "--ff-only", "origin/main")
    case.base = case.history.tip
    model = case.ctx.executor_factory("claude")
    implement = model.run

    def with_shared(**kwargs):
        result = implement(**kwargs)
        (kwargs["worktree"] / SHARED).write_text(PR_SHARED)
        return result

    monkeypatch.setattr(model, "run", with_shared)
    support.queue_workers(case, [N])
    row = next(iter(merge_owner.admissions(case.store).values()))
    job = merge_owner._load(case.ctx, queue.seat(case.ctx, N, row["target"]), row)
    changed = set(case.tracker.pr_changed_files(job["pr"]))
    assert {f"src/member_{N}.py", SHARED} <= changed, changed
    assert case.tracker.pr_checks(job["pr"]) == "pass"
    pushes = []
    push = GitRepo.push_head

    def published(git, *args, **kwargs):
        pushes.append(git.head_sha())
        return push(git, *args, **kwargs)

    monkeypatch.setattr(GitRepo, "push_head", published)
    return job, pushes, len(case.ci.rounds)


def main_file(case, path):
    return git_command(case.history.origin, "show", f"main:{path}")


def log_tail(case):
    return "\n".join(case.store.log_path(N).read_text().splitlines()[-25:])
