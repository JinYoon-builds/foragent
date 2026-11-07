"""Acceptance test for person-count-real (merge owner and merge batch).

A PR that is in a merge batch or the merge-owner queue waits for the merge owner's turn,
not for a person: it is not counted on the dashboard's "N questions to answer" line, nor
listed there or in `fa inspect` as the person's merge decision, even when the issue still
carries a routing to the person (needs-human with a merge-decision reason). On 2026-11-07
#2322 showed as "PR awaits a human merge decision" while it sat in batch #2428.

Real runner, gate tick, merge owner, Git and state of the locked merge-path world; only
GitHub, models and the clock are fake. The person's views are the public
`fa gate watch --once` (the dashboard) and `fa inspect --json`. A PR the owner does not
hold, routed the same way, stays the person's in the same frame.

This file is separate from test_person_count_real.py: that file's view fixtures are
autouse and would change the HOME of the merge-path world.
"""

from datetime import timedelta
import json
import re
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_inspect_merge_queued_not_human import (  # noqa: E402
    CAREFUL, PERSON_GROUPS, QUEUED, bind, gate, owner_world, run_one,  # noqa: F401
)
from test_merge_owner_proto import proto  # noqa: E402, F401
from test_merge_owner_batch import batched  # noqa: E402, F401
from test_batch_scope_fixed import build_waiting_batch  # noqa: E402

MERGE_REASON = "acceptance-touched"   # a person's merge decision (reasons.GROUP: review)
MEMBERS = (1, 5)
OUTSIDE = 2   # approved work that is in no batch


def plain(value):
    value = re.sub(r"\x1b\]8;[^\x1b\x07]*(?:\x1b\\|\x07)", "", value)
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


def person_counts(frame):
    """Every count of the person's questions the dashboard prints (header, bubble)."""
    counts = [int(value) for value in re.findall(r"(\d+) questions? to answer", frame)]
    return counts + [1 for _ in re.findall(r"\bone question to answer", frame, re.I)]


def asked_of_person(frame):
    """Issue numbers whose acorn ('◖█#N word▌' or '[#N word]') reads as a question."""
    found = set()
    for line in plain(frame).splitlines():
        if "▌" not in line and "[" not in line:
            continue
        for match in re.finditer(r"#(\d+)([^▌\]#]*)", line):
            if "question" in match[2].casefold():
                found.add(int(match[1]))
    return sorted(found)


def route_merge_decision(case, number):
    """The issue is routed to the person as a merge decision (label + marker comment)."""
    case.clock[0] += timedelta(seconds=1)
    case.tracker.add_labels(number, ["needs-human"])
    case.tracker.comment(number, f"<!-- foragent:needs-human reason={MERGE_REASON} -->\n"
                                 "**PR awaits a human merge decision**\n\nThe PR changes an acceptance test.")


def observe(case):
    """One gate tick after the gate's issue refresh is due, so it records the new routing."""
    case.clock[0] += timedelta(minutes=10)
    gate(case)


def person_screen(case, capsys, monkeypatch):
    """(dashboard frame, `fa inspect --json` payload) as the person sees them."""
    from foragent import cli

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("COLUMNS", "200")
    capsys.readouterr()
    assert cli.main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    frame = plain(capsys.readouterr().out)
    assert cli.main(["inspect", "--repo-root", str(case.root), "--json"]) == 0
    return frame, json.loads(capsys.readouterr().out)


def person_items(payload, number):
    return [item for item in payload["items"] if item["number"] == number and item["group"] in PERSON_GROUPS]


def assert_counts_match(payload):
    counts = payload["summary"]["counts"]
    for group in PERSON_GROUPS:
        assert counts.get(group, 0) == sum(item["group"] == group for item in payload["items"]), counts


def test_pr_in_the_merge_owner_queue_is_not_a_persons_merge_decision(owner_world, monkeypatch, capsys):
    """Reject counting or listing a merge-queued PR as the person's, while a PR the owner does not hold stays."""
    from foragent import merge_owner

    case = owner_world
    bind(case, monkeypatch, [case.config])
    case.config = replace_config(case, review_paths={f"src/member_{CAREFUL}.py": "careful"})
    assert run_one(case, CAREFUL) != "merge-queued", "Setup: the careful PR stays open for a person"
    assert run_one(case, QUEUED) == "merge-queued", case.store.log_path(QUEUED).read_text()
    assert merge_owner.pending(case.store, QUEUED), "Setup: the merge owner holds the queued PR"
    for number in (QUEUED, CAREFUL):
        route_merge_decision(case, number)
    observe(case)
    assert merge_owner.pending(case.store, QUEUED), "Setup: the PR is still in the merge-owner queue"

    frame, payload = person_screen(case, capsys, monkeypatch)
    assert QUEUED not in asked_of_person(frame), (
        f"A PR in the merge-owner queue is not listed as the person's merge decision:\n{frame}")
    assert CAREFUL in asked_of_person(frame), (
        f"A PR the owner does not hold is still the person's merge decision:\n{frame}")
    assert set(person_counts(frame)) == {1}, (
        f"The person's count is 1 (the careful PR only):\n{frame}")
    assert not person_items(payload, QUEUED), (
        f"`fa inspect` does not list the queued PR as the person's: {person_items(payload, QUEUED)}")
    assert person_items(payload, CAREFUL), "`fa inspect` still lists the careful PR for the person"
    assert_counts_match(payload)


def replace_config(case, **changes):
    from dataclasses import replace

    config = replace(case.config, **changes)
    case.ctx.config = config
    return config


def test_pr_in_a_merge_batch_is_not_a_persons_merge_decision(batched, monkeypatch, capsys):
    """Reject counting or listing a batched PR as the person's, while work outside the batch stays."""
    from foragent import merge_owner

    case = batched
    build_waiting_batch(case, list(MEMBERS))
    bind(case, monkeypatch, [case.config])
    for number in (*MEMBERS, OUTSIDE):
        route_merge_decision(case, number)
    observe(case)
    assert all(merge_owner.pending(case.store, number) for number in MEMBERS), (
        "Setup: the batch members are still held by the merge owner")

    frame, payload = person_screen(case, capsys, monkeypatch)
    listed = asked_of_person(frame)
    for number in MEMBERS:
        assert number not in listed, f"Batched PR #{number} is not listed as the person's:\n{frame}"
        assert not person_items(payload, number), (
            f"`fa inspect` does not list batched #{number} as the person's: {person_items(payload, number)}")
    assert listed == [OUTSIDE], f"Only the work outside the batch is the person's:\n{frame}"
    assert set(person_counts(frame)) == {1}, f"The person's count is 1:\n{frame}"
    assert_counts_match(payload)
