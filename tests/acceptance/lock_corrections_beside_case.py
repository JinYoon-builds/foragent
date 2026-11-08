"""Shared world for lock-corrections-beside-issue (helper, not a test file).

Open issue #7 ("widgets are counted") is in progress: its branch ``foragent/issue-7`` and PR hold
its work on top of main. Its Done when runs one locked test, ``test_widget_count.py``, which imports
the locked helper ``widget_support.py``. Closed issue #5 is done: its locked test
``test_widget_label.py`` (it also imports the helper) is green on main.

Real Git (a local bare origin) holds main and the issue branch. Only the measured test runs are
scripted (``order_prepare_check.run``, the runner every publication measurement uses), keyed on
where the run happened (main, the issue branch) and on the bytes of every locked file in the
measured tree, so a wrong file version or a run in the wrong place reads as an unknown result.
"""

from types import SimpleNamespace

import pytest

from conftest import git_command
from test_order import offline_publication  # noqa: F401
from test_order_publications import case  # noqa: F401
from foragent import order, order_files, order_prepare_check, order_publications
from foragent.order import PlanError
from foragent.scheduler import TickReport

NUMBER = 7
CLOSED_NUMBER = 5
BRANCH = f"foragent/issue-{NUMBER}"
HELPER = "tests/acceptance/widget_support.py"
LOCKED = "tests/acceptance/test_widget_count.py"
CLOSED = "tests/acceptance/test_widget_label.py"
COMMAND = f"uv run pytest -q {LOCKED}"
CLOSED_COMMAND = f"uv run pytest -q {CLOSED}"

# The helper on main is stale (main merged a change of the shared widget fixture); the correction fixes it.
OLD_HELPER = "WIDGETS = ['bolt', 'nut', 'gear']\n"
NEW_HELPER = "WIDGETS = ['bolt', 'nut', 'gear', 'cog']\n"
LOCKED_TEST = ("from widget_support import WIDGETS\n\n\ndef test_widget_count():\n"
               "    assert widget_count(WIDGETS) == 4\n")
OLD_CLOSED = ("from widget_support import WIDGETS\n\n\ndef test_widget_label():\n"
              "    assert widget_label(WIDGETS[0]) == 'bolt'\n")
# Issue #7 changes labels to upper case, contradicting closed issue #5's locked expectation.
NEW_CLOSED = ("from widget_support import WIDGETS\n\n\ndef test_widget_label():\n"
              "    assert widget_label(WIDGETS[0]) == 'BOLT'\n")

GOAL = "Widgets are counted."
SCOPE = "Colours."
STATEMENT = "Counting the widgets reports how many there are."
FILES = ["src/widget.py"]
CLOSED_STATEMENT = "A widget's label is its name."


def _issue(goal, statement, command, locked, test):
    """The issue as the statement route published it: Behaviour section and locked Done when."""
    return {"goal": goal, "statements": [statement], "out_of_scope": SCOPE, "files": list(FILES),
            "done_when": [command], "acceptance_files": {locked: test}}

# #7's own lock corrected (the route #2558 already covers; used only by a guard control).
FIXED_TEST = LOCKED_TEST.replace("widget_count(WIDGETS) == 4", "widget_count(WIDGETS) >= 4")

VERSIONS = {HELPER: {OLD_HELPER: "old", NEW_HELPER: "new"},
            LOCKED: {LOCKED_TEST: "locked", FIXED_TEST: "fixed"},
            CLOSED: {OLD_CLOSED: "old", NEW_CLOSED: "new"}}


def _result(command, nodes, passed):
    if passed:
        return {"command": command, "exit_code": 0, "timed_out": False, "output": "passed", "changed": [],
                "failing_node_ids": [], "collected_node_ids": nodes, "nonred_node_ids": nodes,
                "nonpassing_node_ids": []}
    return {"command": command, "exit_code": 1, "timed_out": False, "output": "failed", "changed": [],
            "failing_node_ids": nodes, "collected_node_ids": nodes, "nonred_node_ids": [],
            "nonpassing_node_ids": nodes}


def _unknown(command):
    return {"command": command, "exit_code": 2, "timed_out": True, "output": "", "changed": [],
            "failing_node_ids": None, "collected_node_ids": None, "nonred_node_ids": None,
            "nonpassing_node_ids": None}


def default_outcomes():
    """Whether each test passes, keyed on (where, test, version of that test, version of the helper)."""
    return {
        # #7's locked test: red on main whatever the helper (#7 is not merged); on the branch it needs
        # the corrected helper.
        ("main", LOCKED, "locked", "old"): False, ("main", LOCKED, "locked", "new"): False,
        ("branch", LOCKED, "locked", "old"): False, ("branch", LOCKED, "locked", "new"): True,
        ("main", LOCKED, "fixed", "old"): False, ("branch", LOCKED, "fixed", "old"): True,
        # Closed #5's locked test: green on main and on the branch's merge base with either helper;
        # its old expectation breaks on #7's branch, the corrected one holds there and fails on main.
        ("main", CLOSED, "old", "old"): True, ("main", CLOSED, "old", "new"): True,
        ("main", CLOSED, "new", "old"): False, ("main", CLOSED, "new", "new"): False,
        ("branch", CLOSED, "old", "old"): False, ("branch", CLOSED, "old", "new"): False,
        ("branch", CLOSED, "new", "old"): True, ("branch", CLOSED, "new", "new"): True,
    }


@pytest.fixture
def world(case, offline_publication, monkeypatch):  # noqa: F811
    root, _config, _tracker, git, _calls, origin = offline_publication
    main_before = git_command(origin, "rev-parse", "refs/heads/main")
    files = {HELPER: OLD_HELPER, LOCKED: LOCKED_TEST, CLOSED: OLD_CLOSED}
    stream = ("commit refs/heads/main\ncommitter Offline Tests <tests@example.com> 1792483200 +0000\n"
              "data 12\nlocked tests\n" f"from {main_before}\n")
    for name, data in files.items():
        stream += f"M 100644 inline {name}\ndata {len(data.encode())}\n{data}\n"
    git_command(origin, "fast-import", "--quiet", input=stream + "\ndone\n")
    main = git_command(origin, "rev-parse", "refs/heads/main")
    work = "def widget_count(widgets):\n    return len(widgets)\n"
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
    repo, directory = case.config.repo, case.config.acceptance_dir
    body = order_files.issue_body(_issue(GOAL, STATEMENT, COMMAND, LOCKED, LOCKED_TEST), repo, "main", directory)
    closed_body = order_files.issue_body(
        _issue("Widgets are labelled.", CLOSED_STATEMENT, CLOSED_COMMAND, CLOSED, OLD_CLOSED), repo, "main",
        directory)
    tracker.add_issue(CLOSED_NUMBER, "Widget labels", closed_body, labels=["approved", "lane:main", "size:s"],
                      state="closed")
    tracker.add_issue(NUMBER, "Widget count", body, labels=["approved", "lane:main", "size:s"])
    pr = tracker.create_pr(head=BRANCH, base="main", title="Widget count", body=f"Closes #{NUMBER}")
    tracker.set_pr_head_sha(pr, branch_head)

    outcomes = default_outcomes()
    runs = []

    def where(base):
        sha = git_command(root, "rev-parse", f"{base}^{{commit}}")
        return "branch" if sha == branch_head else "main" if sha == main else sha

    def version(tree, base, name):
        placed = tree / name
        if placed.is_file():
            data = placed.read_text()
        else:
            try:
                data = git_command(root, "show", f"{base}:{name}") + "\n"
            except Exception:
                return None
        return VERSIONS.get(name, {}).get(data, "unknown")

    def measured(git_, base, context, place, verify, commands, directory, timeout_s, *, collect_node_ids=False):
        tree = directory / "measured"
        tree.mkdir(parents=True, exist_ok=True)
        place(tree)
        spot = where(base)
        helper = version(tree, base, HELPER)
        results = []
        for command in commands:
            tests = []
            for part in command.split():
                name = part.split("::", 1)[0]
                while name.startswith("./"):
                    name = name[2:]
                if name in (LOCKED, CLOSED) and name not in tests:
                    tests.append(name)
            nodes, passed, known = [], True, bool(tests)
            for name in tests:
                test_version = version(tree, base, name)
                runs.append((spot, name, test_version, helper, command))
                outcome = outcomes.get((spot, name, test_version, helper))
                if outcome is None:
                    known = False
                    break
                nodes.append(f"{name}::{name.rsplit('/', 1)[-1][:-3]}")
                passed = passed and outcome
            results.append(_result(command, nodes, passed) if known else _unknown(command))
        return results

    monkeypatch.setattr(order_prepare_check, "run", measured)
    case.ctx.git, case.ctx.repo_root = git, root
    case.root = root
    case.git = git
    return SimpleNamespace(case=case, root=root, git=git, origin=origin, main=main, branch_head=branch_head,
                           outcomes=outcomes, runs=runs, tracker=tracker, pr=pr, body=body,
                           closed_body=closed_body)


def correction(files, plan_id="2026-11-08-widget-lock"):
    """An order edit of open issue #7 that restates it exactly and only carries corrected locked files."""
    edit = {"number": NUMBER, "goal": GOAL, "out_of_scope": SCOPE, "statements": [STATEMENT],
            "files": list(FILES), "acceptance_files": dict(files), "done_when": [COMMAND]}
    return {"id": plan_id, "title": "Widget lock correction", "summary": "Stale locks",
            "spec_markdown": "# Stale locks\n", "issues": [], "edits": [edit]}


def publish_and_check(world, plan, *, approve=True):
    """Publish with --approve (the orchestrator's approval), then run the publication check once.

    Returns the publication request row, or None when ``fa order publish`` refused the plan outright
    (the refusal text is kept on ``world.refusal``)."""
    case = world.case
    world.refusal = None
    try:
        order.publish(plan, world.root, case.config, case.tracker, repo=case.config.repo, git=world.git,
                      approve=approve, approve_numbers=[NUMBER] if approve else [])
    except PlanError as exc:
        world.refusal = str(exc)
        return None
    order_publications.check_pending(case.ctx, TickReport())
    rows = [row for row in order_publications.requests(case.store) if row["plan"]["id"] == plan["id"]]
    assert len(rows) == 1, rows
    return rows[0]


def apply(world, plan):
    order_publications.apply_next(world.case.ctx, TickReport())
    rows = [row for row in order_publications.requests(world.case.store) if row["plan"]["id"] == plan["id"]]
    assert len(rows) == 1, rows
    return rows[0]


def accepted(row):
    return (row is not None and row["status"] in {"pending", "applying", "published"}
            and row.get("red_check_commit") is not None)


def describe(world, row):
    if row is None:
        return f"refused by fa order publish: {world.refusal}"
    return f"{row['status']}: {row.get('reason')}"


def commits_touching(world, ref, name, since=None):
    """How many commits on origin's ``ref`` (after ``since``) change ``name``."""
    span = f"{since}..{ref}" if since else ref
    listed = git_command(world.origin, "rev-list", span, "--", name)
    return len([line for line in listed.splitlines() if line.strip()])


def without_issue_branch(world):
    """Issue #7 has no implementation in progress: no branch on origin and no PR."""
    git_command(world.origin, "update-ref", "-d", f"refs/heads/{BRANCH}")
    git_command(world.root, "update-ref", "-d", f"refs/remotes/origin/{BRANCH}")
    world.tracker.close_pr(world.pr, delete_branch=False)


def on_origin(world, ref, name):
    try:
        return git_command(world.origin, "show", f"{ref}:{name}") + "\n"
    except Exception:
        return None
