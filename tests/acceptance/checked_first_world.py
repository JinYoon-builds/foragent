"""Shared world for checked-pr-lands-first (PM r1438).

Real gate tick, merge owner, runner, Git (bare origin) and state of the locked
merge-path world (`proto` from tests/test_merge_owner_proto.py, `batched` from
tests/test_merge_owner_batch.py); only GitHub, models and the clock are fake.

Queued main writes:
- a role memory write, queued with the public producer
  `main_write_requests.capture` and applied by the owner's real drain;
- an order publication. The real publication queue needs the order-preparation
  world of tests/test_order_publications.py, which has no real merge path. Here
  the owner's publication entry point `order_publications.apply_next` is replaced
  by a stand-in for one queued publication: whenever the owner calls it while the
  publication is still queued, it commits the publication's file to origin's
  main (one commit, like a real publication) and records the main it applied on.
  `order_publications.pending`/`needs_owner` report it while it is queued.

Log lines are the owner's stdout (merge-owner.log in production):
"merge owner checks start: #N at <sha12>".
"""

from pathlib import Path
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from conftest import git_command  # noqa: E402
import test_merge_owner_proto as support  # noqa: E402
from foragent import main_write_requests as writes, merge_owner, order_publications  # noqa: E402

MEMORY = ".claude/agent-memory/fa-docs/MEMORY.md"
MEMORY_TEXT = "Lesson queued while the PR was checked"
PUBLICATION = "src/published_order.py"
PUBLICATION_TEXT = "# Order queued while the PR was checked"
NEIGHBOR = "src/neighbor.py"


def main_tip(case):
    return git_command(case.history.origin, "rev-parse", "refs/heads/main")


def main_text(case, path):
    try:
        return git_command(case.history.origin, "show", f"main:{path}")
    except Exception:
        return None


def is_ancestor(case, older, newer):
    try:
        git_command(case.history.origin, "merge-base", "--is-ancestor", older, newer)
    except Exception:
        return False
    return True


def first_parent(case, sha):
    return git_command(case.history.origin, "rev-parse", f"{sha}^1")


def commits_touching(case, path, since):
    """First-parent commits on main after `since` that change `path`."""
    out = git_command(case.history.origin, "rev-list", "--first-parent", f"{since}..main", "--", path)
    return out.splitlines()


def commit_touching(case, path, since):
    found = commits_touching(case, path, since)
    return found[0] if len(found) == 1 else None


def check_starts(output, n):
    return [line for line in output.splitlines() if line.startswith(f"merge owner checks start: #{n} ")]


def check_ends_ok(output, n):
    return [line for line in output.splitlines()
            if line.startswith(f"merge owner checks end: #{n} ") and " ok " in line]


def issue_heads(case, n):
    """Distinct heads ever pushed to the member's PR branch (each is a new CI round)."""
    return [head for head, row in case.ci.by_head.items() if row.branch == f"foragent/issue-{n}"]


def install(case, monkeypatch):
    """Main pushes by the owner's queued writes start no PR CI; add the publication stand-in."""
    reports = case.ci.reports
    monkeypatch.setattr(case.ci, "reports", lambda row: [] if row.branch == "main" else reports(row))
    case.publication = {"queued": False, "applied_on": [], "calls": 0}

    def queued(store=None):
        return case.publication["queued"]

    def pending(store):
        return [{"id": "checked-first-order", "title": "Checked first order", "status": "pending", "reason": None}] if queued() else []

    def apply_next(ctx, report, *, on_deferred=None, **kwargs):
        case.publication["calls"] += 1
        applied = False
        if queued():
            case.publication["applied_on"].append(main_tip(case))
            count = len(case.publication["applied_on"])
            case.history.tip = main_tip(case)  # The owner's own pushes moved main too.
            case.history.update({PUBLICATION: f"{PUBLICATION_TEXT} ({count})\n"},
                                message="Publish order checked-first")
            case.publication["queued"] = False
            applied = True
            report.actions.append("Order publication applied: checked-first-order")
        if on_deferred is not None:
            on_deferred(None)
        return applied

    monkeypatch.setattr(order_publications, "apply_next", apply_next)
    monkeypatch.setattr(order_publications, "pending", pending)
    monkeypatch.setattr(order_publications, "needs_owner", lambda store: queued())
    monkeypatch.setattr(order_publications, "report_pending", lambda *args, **kwargs: None)


def queue_memory(case, text=MEMORY_TEXT):
    path = case.root / MEMORY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    row = writes.capture(case.git, case.ctx.config, [MEMORY], "Memory", "lane docs", store=case.store)
    assert row is not None and row in writes.pending(case.store), "the memory write is queued for the owner"
    return row


def queue_publication(case):
    case.publication["queued"] = True


def memory_status(case, row):
    return next(item for item in writes.requests(case.store) if item["id"] == row["id"])["status"]


def checked_single_pr(case, capsys, n=5):
    """PR #n is refreshed onto a moved main by the owner; its checks there end ok and its CI passes.

    Returns (pr, head, checked_main): the PR's checked head and the main its checks ran on.
    """
    support.queue_workers(case, [n])
    pr = support.pr_for(case, n)
    case.history.update({NEIGHBOR: "OTHER = 'main moved before the owner checks'\n"},
                        message="Main moves before the owner checks")
    capsys.readouterr()
    support.gate(case)
    out = capsys.readouterr().out
    head = case.tracker.pr_head_sha(pr.number)
    checked_main = main_tip(case)
    assert check_ends_ok(out, n) and any(f"at {head[:12]} ok " in line for line in check_ends_ok(out, n)), (
        f"setup: the owner's checks of #{n} end ok on the current main; log: {out.splitlines()}")
    assert not case.tracker.merged_prs, "setup: the refreshed PR has not merged yet"
    assert is_ancestor(case, checked_main, head), "setup: the checked head is built on the current main"
    assert case.tracker.pr_checks(pr.number) == "pass", "setup: the PR's CI on the checked head passed"
    assert merge_owner.admissions(case.store), "setup: the PR is still merge-queued"
    return pr, head, checked_main


def landed_merge(case, pr):
    return next((row for row in case.merges if row["pr"] == pr.number), None)


__all__ = [
    "MEMORY", "MEMORY_TEXT", "NEIGHBOR", "PUBLICATION", "PUBLICATION_TEXT", "check_ends_ok", "check_starts",
    "checked_single_pr", "commit_touching", "commits_touching", "first_parent", "install", "is_ancestor",
    "issue_heads", "landed_merge", "main_text", "main_tip", "memory_status", "queue_memory",
    "queue_publication", "support",
]
