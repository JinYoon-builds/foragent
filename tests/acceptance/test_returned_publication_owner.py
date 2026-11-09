"""Acceptance test for returned-publication-owner. Locked by Foragent; do not edit.

An order publication that the publication check returns to preparation is the
orchestrator's open work while it remains in preparation. A new attempt closes its
inbox request; another return reopens the same request. The orchestrator
inbox holds exactly one request for it with the full reason; recording "processing" or
"done" on that request does not hide it; an edit-only publication can be corrected and
published again without `fa order prepare`; and a publication left in preparation for
more than two hours without a new attempt shows once in the /fa report.

Real merge owner, publication queue, preparation record and CLI; fake Git, GitHub and
measured test runs from tests/test_order_publications.py. No network, model or signal.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import sys
import time

import pytest
import yaml

from test_order_publications import COMMAND, NAME, case, review_plan, submit  # noqa: F401  (fixture)
from test_skills_autosync import cli_exit

TITLE = "Zephyr ledger import"
T0 = datetime(2026, 11, 7, 5, 0, tzinfo=UTC)
OLD = "uv run pytest tests/test_zephyr_regression.py"
OLD_NODE = "tests/test_zephyr_regression.py::test_old"
LONG_REASON = "Tests passed on current main: the zephyr check is green.\n" + "\n".join(
    f"diagnostic line {index}: tests/acceptance/test_new.py::test_new passed in 0.0{index % 10}s"
    for index in range(120))
SECOND_REASON = "Collection failed on the second attempt: zephyr fixture missing.\n" + "\n".join(
    f"second diagnostic {index}: ImportError while importing zephyr_{index}" for index in range(80))


class Clock(datetime):
    now_value = [T0]

    @classmethod
    def now(cls, tz=None):
        value = cls.now_value[0]
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return cls.now_value[0].replace(tzinfo=None)


@pytest.fixture
def owner(case, monkeypatch):
    """The publication case with a project folder for the CLI and one controlled clock."""
    from foragent import cli
    from foragent.state import StateStore

    clock = Clock.now_value = [T0]
    for name, module in list(sys.modules.items()):
        if (name == "foragent" or name.startswith("foragent.")) and getattr(module, "datetime", None) is datetime:
            monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(time, "time", lambda: clock[0].timestamp())
    factory = StateStore.for_repo.__func__
    monkeypatch.setattr(StateStore, "for_repo", classmethod(
        lambda cls, repo, **kwargs: factory(cls, repo, now=lambda: clock[0])))
    case.store = StateStore(case.store.root, lambda: clock[0])
    case.ctx.store = case.store
    case.ctx.now = lambda: clock[0]
    case.plan["title"] = TITLE
    review_plan(case)  # The record reviewed this title, as a preparation of it would.
    project = case.root.parent / "project"
    project.mkdir()
    (project / ".foragent.yml").write_text(yaml.safe_dump(
        {"repo": case.config.repo, "language": "en", "skill_libraries": [], "merge_owner": True,
         "test_command": case.config.test_command}), encoding="utf-8")
    monkeypatch.setattr(cli.star, "offer", lambda language: None)
    from foragent import order_prepare

    path = order_prepare._record_path(case.store, case.plan)
    case.reviewed = deepcopy(order_prepare._read(path, case.plan))
    case.project, case.clock, case.cli = project, clock, cli
    return case


def run_cli(case, capsys, *argv):
    capsys.readouterr()
    code = cli_exit(case.cli.main, [*argv, "--repo-root", str(case.project)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def inbox(case, capsys, *options):
    code, out, err = run_cli(case, capsys, "orch", "inbox", "--json", *options)
    assert code in (0, None), f"fa orch inbox failed: {err}"
    return json.loads(out)


def report(case, capsys):
    code, out, err = run_cli(case, capsys, "brief", "--report")
    assert code in (0, None), f"fa brief --report failed: {err}"
    return out


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def publication_ids(case):
    from foragent import order_publications

    return {row["id"] for row in order_publications.requests(case.store) if row["plan"]["id"] == case.plan["id"]}


def order_requests(case, rows):
    """Inbox requests about this order's publications, however the request names them."""
    ids = publication_ids(case)
    return [row for row in rows
            if row.get("publication") in ids or any(TITLE in text or text in ids for text in strings(row))]


def carries(request, reason):
    return any(reason in text for text in strings(request))


def owner_passes(case, times=2):
    """What the gate does each tick: off-owner checks, then the merge owner."""
    from foragent import merge_owner, order_publications
    from foragent.scheduler import TickReport

    for _ in range(times):
        report = TickReport()
        order_publications.check_pending(case.ctx, report)
        merge_owner.tick(case.ctx, report)


def reprepare(case):
    """A fresh preparation of the order's new issue, as `fa order prepare` would record."""
    from foragent import order_prepare

    order_prepare._write(order_prepare._record_path(case.store, case.plan), deepcopy(case.reviewed))


def returned(case, reason, *, collection=False):
    """Submit the order and let the publication check return it with ``reason``."""
    case.verdict.clear()
    case.verdict.update(status="hold", reason=reason)
    case.verdict.update({"exit_code": 2, "collected_node_ids": None, "failing_node_ids": None}
                        if collection else {"exit_code": 0})
    submit(case)
    owner_passes(case)


def red(case):
    case.verdict.clear()
    case.verdict.update(status="reviewed", reason="Still red on current main")


def statuses(case):
    from foragent import order_publications

    return [row["status"] for row in order_publications.requests(case.store) if row["plan"]["id"] == case.plan["id"]]


# Statement 1 ---------------------------------------------------------------------------

def restart(case):
    """A new gate process over the same durable folder runs its passes again."""
    from foragent.state import StateStore

    case.store = case.ctx.store = StateStore(case.store.root, lambda: case.clock[0])
    case.clock[0] += timedelta(minutes=5)
    owner_passes(case)


def test_a_return_gives_the_inbox_one_request_with_the_full_reason_even_after_a_restart(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    assert statuses(case) == ["preparation"]
    restart(case)
    first = order_requests(case, inbox(case, capsys))
    assert len(first) == 1 and carries(first[0], LONG_REASON), "One request with the full reason after a restart"
    # The order returns again from a restarted gate, then the gate restarts once more.
    reprepare(case)
    returned(case, SECOND_REASON, collection=True)
    assert statuses(case)[-1] == "preparation"
    restart(case)
    restart(case)
    # Every request about this order, whatever its status: a second request that the gate
    # opened and another that it closed or superseded are still two requests.
    found = order_requests(case, inbox(case, capsys))
    assert len(found) == 1, (
        f"Across restarts and a second return the inbox holds exactly one request for the order, "
        f"not {[(row['id'], row.get('status')) for row in found]}")
    assert found[0]["id"] == first[0]["id"], "The second return reuses the request the first return opened"
    assert carries(found[0], SECOND_REASON), "The request must carry the full reason of the latest return"
    pending = order_requests(case, inbox(case, capsys, "--pending"))
    assert [row["id"] for row in pending] == [found[0]["id"]], "That one request is the order's open work"




# Statement 2 ---------------------------------------------------------------------------

@pytest.mark.parametrize("recorded", ["processing", "done"])
def test_the_request_shows_open_whatever_was_recorded_while_in_preparation(owner, capsys, recorded):
    case = owner
    returned(case, LONG_REASON)
    request, = order_requests(case, inbox(case, capsys))
    # The orchestrator records something; the result of recording it is not the point.
    run_cli(case, capsys, "orch", "request-status", request["id"], recorded,
            "--note", "waiting for the PM", "--answer", "republish later")
    case.clock[0] += timedelta(minutes=10)
    owner_passes(case)
    shown = order_requests(case, inbox(case, capsys, "--pending"))
    assert len(shown) == 1, f"After recording {recorded!r} the request must still be listed as open work"
    assert shown[0]["status"] in {"new", "open"}, (
        f"While its publication is in preparation the request shows as open, not {shown[0]['status']!r}")




# Statement 3 ---------------------------------------------------------------------------

def edit_only(case, monkeypatch):
    """An edit-only order: one statement edit of an existing issue, no new issues."""
    from foragent import order, order_prepare, order_prepare_check, order_prepare_cycle, order_prepare_setup

    number = case.tracker.create_issue("Existing zephyr work", f"## Done when\n```sh\n{OLD}\n```\n", ["lane:main"])
    edit = {"number": number, "goal": "Import zephyr ledgers", "out_of_scope": "Other ledgers",
            "statements": ["A zephyr ledger imports"], "done_when": [OLD, COMMAND],
            "acceptance_files": {NAME: "def test_new():\n    assert False\n"}}
    case.plan["issues"] = []
    case.plan["edits"] = [edit]
    files = order.validate_plan(case.plan, case.root, case.config)
    view = order.edit_view(edit, case.tracker.get_issue(number), case.config)
    entry = {"key": view["key"], "status": "reviewed", "phase": "reviewing_tests", "reason": "Red",
             "written": edit["acceptance_files"], "done_when": list(edit["done_when"]), "base_revision": "main-1",
             "cycle": order_prepare_cycle.identity(case.plan, view, case.config, case.root, files),
             "published": order_prepare_setup.published_digest(case.plan, case.root, case.config, files),
             "others": {}, "attempts": []}
    order_prepare._write(order_prepare._record_path(case.store, case.plan),
                         {"plan": case.plan["id"], "issues": {view["key"]: entry}})

    def measured(git, base, context, place, verify, commands, directory, timeout_s, *, collect_node_ids=False):
        runs = []
        for command in commands:
            new = command == COMMAND
            nodes = [f"{NAME}::test_new"] if new else [OLD_NODE]
            runs.append({"command": command, "exit_code": 1 if new else 0, "timed_out": False,
                         "output": f"{command}: {'1 failed' if new else '1 passed'}", "changed": [],
                         "failing_node_ids": nodes if new else [], "collected_node_ids": nodes,
                         "nonred_node_ids": [] if new else nodes})
        case.checks.append(list(commands))
        return runs

    monkeypatch.setattr(order_prepare_check, "run", measured)
    monkeypatch.setattr(order_prepare_cycle, "prepare_issue",
                        lambda *args, **kwargs: pytest.fail("An edit-only correction must not start a preparation"))
    return number, edit


def test_a_returned_edit_only_publication_is_corrected_and_published_without_preparation(owner, capsys, monkeypatch):
    from foragent import order

    case = owner
    number, edit = edit_only(case, monkeypatch)
    submit(case)
    owner_passes(case)
    assert statuses(case) == ["preparation"], "The regression-only line passes on main, so the check returns it"
    # The orchestrator's correction: drop the regression-only line that already passes on main.
    edit["done_when"] = [COMMAND]
    edit["done_when_removals"] = [{"line": OLD, "reason": "PR CI guards regressions"}]
    case.clock[0] += timedelta(minutes=20)
    try:
        submit(case)
    except order.PlanError as exc:
        pytest.fail(f"A corrected edit-only publication must publish again without fa order prepare: {exc}")
    owner_passes(case)
    assert statuses(case)[-1] == "published", "The corrected edit-only publication is published again"
    body = case.tracker.get_issue(number).body
    assert COMMAND in body, "The published edit carries its corrected Done when"
    assert order_requests(case, inbox(case, capsys, "--pending")) == [], (
        "Once the edit is published again its orchestrator request is closed")


# Statement 4 ---------------------------------------------------------------------------

def test_a_publication_left_in_preparation_over_two_hours_appears_once_in_the_report(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    case.clock[0] = T0 + timedelta(hours=1, minutes=55)
    owner_passes(case, times=1)
    assert TITLE not in report(case, capsys), "Two hours have not passed yet: it is not waiting work yet"
    case.clock[0] = T0 + timedelta(hours=2, minutes=5)
    owner_passes(case, times=1)
    first = report(case, capsys)
    assert first.count(TITLE) == 1, f"Past two hours it appears exactly once as waiting work:\n{first}"
    case.clock[0] += timedelta(minutes=30)
    assert report(case, capsys).count(TITLE) == 1, "It stays listed once on a later report"


def test_a_new_attempt_restarts_the_two_hours_and_two_returns_still_appear_once(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    case.clock[0] = T0 + timedelta(hours=1, minutes=30)
    reprepare(case)
    returned(case, SECOND_REASON, collection=True)
    assert statuses(case)[-1] == "preparation"
    case.clock[0] = T0 + timedelta(hours=2, minutes=30)
    owner_passes(case, times=1)
    assert TITLE not in report(case, capsys), "A new attempt one hour ago: not yet waiting work"
    case.clock[0] = T0 + timedelta(hours=3, minutes=35)
    owner_passes(case, times=1)
    shown = report(case, capsys)
    assert shown.count(TITLE) == 1, f"Two returns of one order still appear once:\n{shown}"


def test_a_publication_published_again_is_not_waiting_work(owner, capsys):
    case = owner
    returned(case, LONG_REASON)
    case.clock[0] = T0 + timedelta(hours=2, minutes=5)
    owner_passes(case, times=1)
    assert report(case, capsys).count(TITLE) == 1
    reprepare(case)
    red(case)
    submit(case)
    owner_passes(case)
    assert statuses(case)[-1] == "published"
    case.clock[0] += timedelta(hours=3)
    assert TITLE not in report(case, capsys), "A publication published again is no longer waiting work"

# test_a_second_return_keeps_one_request... moved to test_returned_publication_owner_guard.py (passes on main;
# PR CI guards regressions, q51). test_the_request_closes_only_when_the_publication_is_published_again removed:
# PM q65 also closes the request on a new attempt; test_returned_publication_request_lifecycle.py covers closing.
