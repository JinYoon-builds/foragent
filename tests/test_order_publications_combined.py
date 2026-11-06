"""The off-owner checker certifies the same request set and bytes the owner lands."""

from dataclasses import replace
import shlex
import sys

import pytest

from test_order import offline_publication  # noqa: F401
from test_order_publications import case, three_requests  # noqa: F401
from foragent import order, order_prepare_check, order_prepare_cycle, order_prepare_setup, order_publications
from foragent import run_base_move
from foragent.git import GitRepo
from foragent.scheduler import TickReport


MEASURE = order_prepare_check.run


def test_owner_waits_for_exact_combined_certification(case, monkeypatch):
    three_requests(case)
    with monkeypatch.context() as patch:
        patch.setattr(order_publications, "_certify_pending", lambda *args: None)
        order_publications.check_pending(case.ctx, TickReport())
    assert len(case.checks) == 3
    order_publications.apply_next(case.ctx, TickReport())
    assert not case.events and len(order_publications.pending(case.store)) == 3

    order_publications.check_pending(case.ctx, TickReport())
    rows = order_publications.pending(case.store)
    identities = sorted(row["id"] for row in rows)
    files = {name for row in rows for name in row["files"]}
    assert all(row["red_check_set"] == {"requests": identities, "commit": "main-1"} for row in rows)
    assert len(case.checks) == 6
    assert all(set(check[3]) == files for check in case.checks[3:])
    monkeypatch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Owner ran tests"))
    order_publications.apply_next(case.ctx, TickReport())
    assert all(row["status"] == "published" for row in order_publications.requests(case.store))
    assert len([event for event in case.events if event[0] == "commit"]) == 1
    assert len([event for event in case.events if event[0] == "push"]) == 1


@pytest.mark.parametrize("grouped", [False, True])
def test_neutral_successors_reuse_checks_without_reserving_requests(case, git_history, monkeypatch, grouped):
    if grouped:
        three_requests(case)
    else:
        from test_order_publications import submit

        submit(case)
    first = git_history.tip
    case.git.remote.head = first
    order_publications.check_pending(case.ctx, TickReport())
    before = order_publications.requests(case.store)
    git_history.update({".claude/agent-memory/api/MEMORY.md": "first memory\n"})
    middle = git_history.tip
    git_history.update({".claude/agent-memory/api/MEMORY.md": "second memory\n"})
    target = git_history.tip
    real_git = GitRepo(git_history.clone)
    real_git.fetch("origin", "main")
    read = run_base_move._git_bytes
    monkeypatch.setattr(run_base_move, "_git_bytes", lambda git, args: read(real_git, args))
    case.git.remote.parents.update({middle: first, target: middle})
    case.git.remote.head = target
    with monkeypatch.context() as patch:
        patch.setattr(order_publications, "_request_owner", lambda *a: pytest.fail("Redundant request reservation"))
        patch.setattr(order_publications, "_checkout", lambda *a: pytest.fail("Redundant checker checkout"))
        patch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Redundant red check"))
        order_publications.check_pending(case.ctx, TickReport())
    assert order_publications.requests(case.store) == before
    order_publications.apply_next(case.ctx, TickReport())
    assert all(row["status"] == "published" for row in order_publications.requests(case.store))
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_combined_certificate_does_not_cover_a_result_affecting_successor(case, git_history, monkeypatch):
    three_requests(case)
    first = git_history.tip
    case.git.remote.head = first
    order_publications.check_pending(case.ctx, TickReport())
    # The net diff is empty; both intervening commits must still be inspected.
    git_history.update({"src/app.py": "temporary change\n"})
    middle = git_history.tip
    git_history.update({"src/app.py": None})
    target = git_history.tip
    real_git = GitRepo(git_history.clone)
    real_git.fetch("origin", "main")
    read = run_base_move._git_bytes
    monkeypatch.setattr(run_base_move, "_git_bytes", lambda git, args: read(real_git, args))
    case.git.remote.parents.update({middle: first, target: middle})
    case.git.remote.head = target
    # Even renewed individual checks cannot renew the old combined certificate.
    for row in order_publications.pending(case.store):
        row["red_check_commit"] = target
        order_publications._save(case.store, row)
    order_publications.apply_next(case.ctx, TickReport())
    assert not case.events and len(order_publications.pending(case.store)) == 3
    order_publications.check_pending(case.ctx, TickReport())
    order_publications.apply_next(case.ctx, TickReport())
    assert all(row["status"] == "published" for row in order_publications.requests(case.store))
    assert len([event for event in case.events if event[0] == "push"]) == 1


def test_individually_red_requests_that_turn_green_together_land_in_later_single_passes(
        case, offline_publication, git_history, monkeypatch):
    rows = three_requests(case)
    rows[-1]["status"] = "pm-held"
    order_publications._save(case.store, rows[-1])
    root, _config, _tracker, git, calls, _origin = offline_publication
    fixture = "docs/design/mode.txt"
    git_history.update({fixture: "off\n"})
    git.fetch("origin", "main")
    target = git.ref_sha("origin/main")
    case.ctx.git, case.ctx.repo_root = git, root
    case.ctx.config = replace(case.config, test_command=f"{shlex.quote(sys.executable)} -m pytest -p no:xdist")
    for index, row in enumerate(rows[:2]):
        item = row["plan"]["issues"][0]
        name = next(iter(item["acceptance_files"]))
        source = ("from pathlib import Path\n"
                  f"def test_new(): assert Path({fixture!r}).read_text() == 'on\\n'\n"
                  if index == 0 else "def test_new(): assert False\n")
        item.update(done_when=[f"{case.ctx.config.test_command} -p no:cacheprovider {name}"],
                    acceptance_files={name: source})
        if index == 1:
            row["plan"]["reference_files"] = {fixture: "on\n"}
        row["files"] = order.validate_plan(row["plan"], root, case.ctx.config)
        row["prepared"]["new"].update(
            written={name: source}, done_when=item["done_when"], base_revision=target,
            failing_node_ids=[f"{name}::test_new"],
            cycle=order_prepare_cycle.identity(row["plan"], item, case.ctx.config, root, row["files"]),
            published=order_prepare_setup.published_digest(row["plan"], root, case.ctx.config, row["files"]))
        order_publications._save(case.store, row)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    measured = []

    def measure(*args, **kwargs):
        result = MEASURE(*args, **kwargs)
        measured.append(result)
        return result

    monkeypatch.setattr(order_prepare_check, "run", measure)
    order_publications.check_pending(case.ctx, TickReport())
    pending = order_publications.pending(case.store)
    assert len(pending) == 2 and all(row["red_check_single_only"] for row in pending)
    assert all(row["red_check_commit"] == target and not row.get("red_check_set") for row in pending)
    assert [result[0]["exit_code"] for result in measured] == [1, 1, 0, 1]
    for index in range(2):
        with monkeypatch.context() as patch:
            patch.setattr(order_prepare_check, "run", lambda *a, **kw: pytest.fail("Owner ran tests"))
            order_publications.apply_next(case.ctx, TickReport())
        assert sum(row["status"] == "published" for row in order_publications.requests(case.store)) == index + 1
        if index == 0:
            assert len(order_publications.pending(case.store)) == 1
            order_publications.check_pending(case.ctx, TickReport())
    assert len(case.tracker.list_issues()) == 4
    assert len([args for args in calls if "commit" in args]) == 2
    assert len([args for args in calls if args[0] == "push"]) == 2
