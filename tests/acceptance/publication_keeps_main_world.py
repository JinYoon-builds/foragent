"""Shared world for publication-keeps-main-changes (r1278). Locked by Foragent; do not edit.

An order carries a lock-only correction of locked acceptance files of open issue #7. It is
published through the merge owner's queue (``order.publish`` with ``merge_owner`` on, the
off-owner red check ``order_publications.check_pending`` and the owner's
``order_publications.apply_next``), the path that wrote the order's file bytes as they were
over a later main (dc672237 erased #2813's change).

Real Git holds main and the issue branch in a local bare origin; the order is prepared and
red-checked on main ``M1``. A test may then move main (``move_main``) before the owner lands
the order. Only the measured pytest runs are scripted: a run passes when the measured locked
test holds the corrected count and fails otherwise, wherever it runs, so the red check accepts
whatever bytes an implementation measures as long as the order's correction is in them.
"""

import subprocess
from types import SimpleNamespace

import pytest

from conftest import git_command
from test_order import offline_publication  # noqa: F401
from test_order_publications import case  # noqa: F401
from foragent import order, order_prepare_check, order_publications
from foragent.scheduler import TickReport

NUMBER = 7
BRANCH = f"foragent/issue-{NUMBER}"
LOCKED = "tests/acceptance/test_widget_count.py"
OTHER = "tests/acceptance/test_widget_names.py"
CORRECTED = "assert widget_count() == 4"
STALE = "assert widget_count() == 3"

# The locked file has two parts far apart: the count test the order corrects (top) and a
# colour table main may change later (bottom). Unchanged lines keep the parts apart.
HEAD_PART = ("from widgets import widget_count\n"
             "\n"
             "\n"
             "def test_widget_count():\n"
             "    {count}\n")
MIDDLE = "".join(f"# unchanged line {n}\n" for n in range(1, 9))
TAIL_PART = ("\n"
             "COLOURS = {colours}\n"
             "\n"
             "\n"
             "def test_colours_known():\n"
             "    assert len(COLOURS) == {size}\n")


def locked_text(*, count: str = STALE, colours: str = '["red", "blue"]', size: int = 2) -> str:
    return HEAD_PART.format(count=count) + MIDDLE + TAIL_PART.format(colours=colours, size=size)


BASE = locked_text()                                                     # main at preparation
ORDER = locked_text(count=CORRECTED)                                     # the order's correction (top)
MAIN_TAIL = locked_text(colours='["red", "blue", "green"]', size=3)      # main's later change (bottom)
BOTH = locked_text(count=CORRECTED, colours='["red", "blue", "green"]', size=3)
MAIN_TOP = locked_text(count="assert widget_count() == 5  # main's own fix")  # overlaps the order

OTHER_BASE = ("from widgets import widget_names\n\n\n"
              "def test_widget_names():\n    assert widget_names() == ['a', 'b', 'c']  # stale\n")
OTHER_ORDER = ("from widgets import widget_names\n\n\n"
               "def test_widget_names():\n    assert widget_names() == ['a', 'b', 'c', 'd']  # " + CORRECTED + "\n")

GOAL = "Widgets are counted."
SCOPE = "Colours."


def _commit(origin, ref, parent, message, files, when):
    stream = [f"commit {ref}\n", f"committer Offline Tests <tests@example.com> {when} +0000\n",
              f"data {len(message.encode())}\n{message}\n", f"from {parent}\n"]
    for name, data in files.items():
        stream.append(f"M 100644 inline {name}\ndata {len(data.encode())}\n{data}\n")
    git_command(origin, "fast-import", "--quiet", input="".join(stream) + "\ndone\n")
    return git_command(origin, "rev-parse", ref)


def blob(repository, revision, name):
    """Exact bytes of ``name`` at ``revision`` (no stripping), or None when absent."""
    done = subprocess.run(["git", "cat-file", "blob", f"{revision}:{name}"], cwd=repository,
                          capture_output=True)
    return done.stdout.decode("utf-8") if done.returncode == 0 else None


def commands(files):
    return [f"uv run pytest -q {name}" for name in files]


def body(files):
    blocks = "".join(f"```sh\n{command}\n```\n" for command in commands(files))
    return f"## Goal\n{GOAL}\n\n## Out of scope\n{SCOPE}\n\n## Done when\n{blocks}"


def _passes(data: str) -> bool:
    return CORRECTED in data


def make_world(case, offline_publication, monkeypatch, files):  # noqa: F811
    """Main ``M1`` holds ``files`` (name -> stale bytes); issue #7's branch holds its work on top."""
    root, _config, _tracker, git, _calls, origin = offline_publication
    start = git_command(origin, "rev-parse", "refs/heads/main")
    main = _commit(origin, "refs/heads/main", start, "locked tests", files, 1792483200)
    branch_head = _commit(origin, f"refs/heads/{BRANCH}", main, "issue 7 work",
                          {"src/widgets.py": "def widget_count():\n    return 4\n"}, 1792483300)
    git_command(root, "fetch", "origin", "main", BRANCH)
    git_command(root, "update-ref", "HEAD", main)
    git_command(root, "read-tree", "--reset", "-u", "HEAD")
    git_command(root, "update-ref", f"refs/remotes/origin/{BRANCH}", branch_head)

    tracker = case.tracker
    tracker.add_issue(NUMBER, "Widget count", body(files), labels=["approved", "lane:main", "size:s"])
    pr = tracker.create_pr(head=BRANCH, base="main", title="Widget count", body=f"Closes #{NUMBER}")
    tracker.set_pr_head_sha(pr, branch_head)
    runs = []

    def measured(git_, base, context, place, verify, commands_, directory, timeout_s, *, collect_node_ids=False):
        tree = directory / "measured"
        tree.mkdir(parents=True, exist_ok=True)
        place(tree)
        results = []
        for command in commands_:
            for name in [part for part in command.split() if part.endswith(".py")]:
                placed = tree / name
                data = placed.read_text() if placed.is_file() else (blob(root, base, name) or "")
                runs.append((base, name, data))
                nodes = [f"{name}::test_{name.rsplit('_', 1)[-1].removesuffix('.py')}"] if data else []
                ok = _passes(data)
                results.append({"command": command, "exit_code": 0 if ok else 1, "timed_out": False,
                                "output": "passed" if ok else "failed", "changed": [],
                                "failing_node_ids": [] if ok else nodes, "collected_node_ids": nodes,
                                "nonred_node_ids": nodes if ok else []})
        return results

    monkeypatch.setattr(order_prepare_check, "run", measured)
    case.ctx.git, case.ctx.repo_root = git, root
    case.root = root
    case.git = git
    return SimpleNamespace(case=case, root=root, git=git, origin=origin, main=main, branch_head=branch_head,
                           tracker=tracker, pr=pr, runs=runs, files=dict(files))


@pytest.fixture
def world(case, offline_publication, monkeypatch):  # noqa: F811
    return make_world(case, offline_publication, monkeypatch, {LOCKED: BASE})


@pytest.fixture
def world_two(case, offline_publication, monkeypatch):  # noqa: F811
    return make_world(case, offline_publication, monkeypatch, {LOCKED: BASE, OTHER: OTHER_BASE})


def correction(world, files, *, plan_id="2026-11-09-widget-lock"):
    # Goal, Out of scope and Done when stay exactly as the open issue has them: only the tests change.
    edit = {"number": NUMBER, "goal": GOAL, "out_of_scope": SCOPE,
            "acceptance_files": dict(files), "done_when": commands(world.files)}
    return {"id": plan_id, "title": "Widget count lock correction", "summary": "Stale count",
            "spec_markdown": "# Stale count\n", "issues": [], "edits": [edit]}


def prepare(world, plan):
    """Publish the order into the owner's queue and red-check it on the current main (M1)."""
    case = world.case
    order.publish(plan, world.root, case.config, case.tracker, repo=case.config.repo, git=world.git,
                  approve_numbers=[NUMBER])
    order_publications.check_pending(case.ctx, TickReport())
    row = request(world, plan)
    assert row["status"] in {"pending", "applying"} and row.get("red_check_commit") == world.main, (
        "Setup: the order must stand red-checked on M1 before main moves; "
        f"got {row['status']}: {row.get('reason')}")
    return row


def request(world, plan):
    row, = [row for row in order_publications.requests(world.case.store) if row["plan"]["id"] == plan["id"]]
    return row


def move_main(world, files, message="main changes a locked file"):
    """Another writer lands ``files`` on origin main after the order was prepared."""
    parent = git_command(world.origin, "rev-parse", "refs/heads/main")
    return _commit(world.origin, "refs/heads/main", parent, message, files, 1792483400)


def land(world, plan, passes=3):
    """Run the gate's checker and the owner until the request settles (or ``passes`` run out)."""
    for _ in range(passes):
        order_publications.check_pending(world.case.ctx, TickReport())
        order_publications.apply_next(world.case.ctx, TickReport())
        if request(world, plan)["status"] not in {"pending", "applying"}:
            break
    return request(world, plan)


def origin_main(world, name):
    return blob(world.origin, "refs/heads/main", name)
