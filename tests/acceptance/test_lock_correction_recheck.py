"""Acceptance test for lock-correction-recheck. Locked by Foragent; do not edit.

The publication check requires every Done when command of an order to fail on main. A
lock-only correction (an order edit that fixes a stale locked-test expectation of an open
issue, purpose unchanged) passes on main by nature. The check accepts such a correction on
evidence from the issue branch it unblocks instead: the corrected tests pass on that branch
head and the previous version of the same tests fails there, and the orchestrator approves it
without the PM. The existing refusals (corrected tests fail on the branch, previous version
passes there, unknown evidence, new-behaviour tests passing on the base) stay as they are and
are guarded by PR CI, so they have no test here.

Real Git (a local bare origin) holds main and the issue branch, so every way of reading the
branch head agrees. Only the measured test runs are scripted, keyed on the commit they run on
and on the test bytes in the measured tree.
"""

from types import SimpleNamespace

import pytest

from conftest import git_command
from test_order import offline_publication  # noqa: F401
from test_order_publications import case  # noqa: F401
from foragent import order, orch_inbox, order_prepare_check, order_publications
from foragent.scheduler import TickReport

NUMBER = 7
BRANCH = f"foragent/issue-{NUMBER}"
LOCKED = "tests/acceptance/test_widget_count.py"
COMMAND = f"uv run pytest -q {LOCKED}"
OLD = "def test_widget_count():\n    assert widget_count() == 3  # main merged a fourth widget\n"
NEW = "def test_widget_count():\n    assert widget_count() == 4\n"
BODY = ("## Goal\nWidgets are counted.\n\n## Out of scope\nColours.\n\n"
        f"## Done when\n```sh\n{COMMAND}\n```\n")


def _pass(command, nodes):
    return {"command": command, "exit_code": 0, "timed_out": False, "output": "passed", "changed": [],
            "failing_node_ids": [], "collected_node_ids": nodes, "nonred_node_ids": nodes}


def _fail(command, nodes):
    return {"command": command, "exit_code": 1, "timed_out": False, "output": "failed", "changed": [],
            "failing_node_ids": nodes, "collected_node_ids": nodes, "nonred_node_ids": []}


def _unreadable(command, nodes):
    return {"command": command, "exit_code": 2, "timed_out": True, "output": "", "changed": [],
            "failing_node_ids": None, "collected_node_ids": None, "nonred_node_ids": None}


@pytest.fixture
def world(case, offline_publication, monkeypatch):  # noqa: F811
    """Main holds the stale locked test; issue #7's branch holds its work on top of main."""
    root, _config, _tracker, git, _calls, origin = offline_publication
    main_before = git_command(origin, "rev-parse", "refs/heads/main")
    stream = ("commit refs/heads/main\ncommitter Offline Tests <tests@example.com> 1792483200 +0000\n"
              "data 11\nlocked test\n" f"from {main_before}\n"
              f"M 100644 inline {LOCKED}\ndata {len(OLD.encode())}\n{OLD}\n\ndone\n")
    git_command(origin, "fast-import", "--quiet", input=stream)
    main = git_command(origin, "rev-parse", "refs/heads/main")
    work = "def widget_count():\n    return 4\n"
    stream = (f"commit refs/heads/{BRANCH}\ncommitter Offline Tests <tests@example.com> 1792483300 +0000\n"
              "data 13\nissue 7 work\n" f"from {main}\n"
              f"M 100644 inline src/widget.py\ndata {len(work.encode())}\n{work}\n\ndone\n")
    git_command(origin, "fast-import", "--quiet", input=stream)
    branch_head = git_command(origin, "rev-parse", f"refs/heads/{BRANCH}")
    git_command(root, "fetch", "origin", "main", BRANCH)
    git_command(root, "update-ref", "HEAD", main)
    git_command(root, "read-tree", "--reset", "-u", "HEAD")
    git_command(root, "update-ref", f"refs/remotes/origin/{BRANCH}", branch_head)

    tracker = case.tracker
    tracker.add_issue(NUMBER, "Widget count", BODY, labels=["approved", "lane:main", "size:s"])
    pr = tracker.create_pr(head=BRANCH, base="main", title="Widget count", body=f"Closes #{NUMBER}")
    tracker.set_pr_head_sha(pr, branch_head)

    # How each scripted run ends, keyed on (where it ran, which version of the locked test it ran).
    outcomes = {("main", "old"): _fail, ("main", "new"): _pass,
                ("branch", "old"): _fail, ("branch", "new"): _pass}
    runs = []

    def where(base):
        sha = git_command(root, "rev-parse", f"{base}^{{commit}}")
        return "branch" if sha == branch_head else "main" if sha == main else sha

    def measured(git_, base, context, place, verify, commands, directory, timeout_s, *, collect_node_ids=False):
        tree = directory / "measured"
        tree.mkdir(parents=True, exist_ok=True)
        place(tree)
        results = []
        for command in commands:
            names = [part for part in command.split() if part.endswith(".py")]
            for name in names:
                placed = tree / name
                if placed.is_file():
                    data = placed.read_text()
                else:
                    try:
                        data = git_command(root, "show", f"{base}:{name}") + "\n"
                    except Exception:
                        data = ""
                version = {OLD: "old", NEW: "new"}.get(data, "unknown")
                spot = where(base)
                runs.append((spot, version, command))
                nodes = [f"{name}::{data.split('(')[0].removeprefix('def ')}"] if data else []
                outcome = outcomes.get((spot, version), _unreadable)
                results.append(outcome(command, nodes))
        return results

    monkeypatch.setattr(order_prepare_check, "run", measured)
    case.ctx.git, case.ctx.repo_root = git, root
    case.root = root
    case.git = git
    return SimpleNamespace(case=case, root=root, git=git, origin=origin, main=main, branch_head=branch_head,
                           outcomes=outcomes, runs=runs, tracker=tracker, pr=pr)


def correction(world, files=None, **extra):
    # Goal, Out of scope and Done when stay exactly as the open issue has them: only the test changes.
    edit = {"number": NUMBER, "goal": "Widgets are counted.", "out_of_scope": "Colours.",
            "acceptance_files": files or {LOCKED: NEW}, "done_when": [COMMAND], **extra}
    return {"id": "2026-11-07-widget-lock", "title": "Widget count lock correction", "summary": "Stale count",
            "spec_markdown": "# Stale count\n", "issues": [], "edits": [edit]}


def publish_and_check(world, plan):
    case = world.case
    order.publish(plan, world.root, case.config, case.tracker, repo=case.config.repo, git=world.git,
                  approve_numbers=[NUMBER])
    order_publications.check_pending(case.ctx, TickReport())
    row, = [row for row in order_publications.requests(case.store) if row["plan"]["id"] == plan["id"]]
    return row


def accepted(row):
    return row["status"] in {"pending", "applying", "published"} and row.get("red_check_commit") is not None


def test_lock_only_correction_proved_on_the_issue_branch_is_accepted(world):
    row = publish_and_check(world, correction(world))
    assert accepted(row), (
        "A lock-only correction whose corrected tests pass on the issue branch head and whose previous "
        f"version fails there must pass the publication check; got {row['status']}: {row.get('reason')}")
    assert ("branch", "new") in {(spot, version) for spot, version, _ in world.runs}, (
        "The corrected tests must be measured on the issue's current branch head")
    assert ("branch", "old") in {(spot, version) for spot, version, _ in world.runs}, (
        "The previous version of the same tests must be measured on the issue's current branch head")


def test_accepted_correction_lands_and_publishes_the_corrected_test(world):
    row = publish_and_check(world, correction(world))
    assert accepted(row), row.get("reason")
    order_publications.apply_next(world.case.ctx, TickReport())
    row, = order_publications.requests(world.case.store)
    assert row["status"] == "published", row.get("reason")
    assert git_command(world.origin, "show", f"main:{LOCKED}") + "\n" == NEW


def test_accepted_correction_is_approved_without_the_pm(world):
    # The orchestrator publishes the correction with --approve-numbers; no PM or person answer is involved.
    row = publish_and_check(world, correction(world))
    assert accepted(row), (
        f"The lock-only correction must pass the check; got {row['status']}: {row.get('reason')}")
    order_publications.apply_next(world.case.ctx, TickReport())
    row, = order_publications.requests(world.case.store)
    assert row["status"] == "published", row.get("reason")
    labels = set(world.tracker.get_issue(NUMBER).labels)
    assert "approved" in labels, f"The corrected issue must stand approved again; labels {labels}"
    assert "needs-human" not in labels, f"The correction must not wait on a person; labels {labels}"
    assert orch_inbox.questions(world.case.store) == [], (
        "Approving the correction must not raise a question for the PM or the person")
