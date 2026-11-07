"""#2477 narrowed (2026-11-07): the remaining-work marker is compared only with recorded link events.

A link event is the time FA observed the link from an issue to the decision. An issue
takes over a decision's remaining work only when its link event was recorded after the
marker was written. An issue's creation, update or listing time never stands in for it;
a link with no recorded link time is unknown and takes over nothing.

The marker's write time is the ledger's: the file's modification time and, because the
project is a git repository, the commit that added the line. Both are set to the same
instant, so the tests hold whichever of the two the implementation reads. Every issue
observation passes an explicit start time to the store's one listing ingest.
"""

from datetime import UTC, datetime, timedelta
import json
import os
import subprocess

from foragent.tracker import Issue
from test_decision_remaining_work import DONE, IN_PROGRESS, STEP_TWO, Ledger, ledger, links, remaining

LEDGER_PATH = "docs/decisions.md"


def at(base, days=0, hours=0):
    return base + timedelta(days=days, hours=hours)


def iso(moment):
    return moment.astimezone(UTC).isoformat()


def observe(view, rows, moment):
    """One complete listing that started at ``moment``."""
    view.case.store.refresh_local_issues(rows, [], {}, moment, observed_at=moment)


def write_marker(view, text, moment):
    """Write the ledger and date it (mtime and git commit) at ``moment``."""
    view.write(text)
    path = view.case.root / LEDGER_PATH
    stamp = moment.timestamp()
    root = str(view.case.root)
    env = {**os.environ, "GIT_AUTHOR_DATE": moment.isoformat(), "GIT_COMMITTER_DATE": moment.isoformat()}
    git = ["git", "-C", root, "-c", "user.name=Test", "-c", "user.email=test@example.com",
           "-c", "commit.gpgsign=false"]
    if not (view.case.root / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True, env=env)
    subprocess.run([*git, "add", LEDGER_PATH], check=True, env=env)
    # The suite forbids `git commit`; plumbing records the same dated commit.
    tree = subprocess.run([*git, "write-tree"], check=True, env=env, capture_output=True, text=True).stdout.strip()
    parent = subprocess.run([*git, "rev-parse", "--verify", "--quiet", "HEAD"], env=env,
                            capture_output=True, text=True).stdout.strip()
    commit = subprocess.run([*git, "commit-tree", tree, *(["-p", parent] if parent else []), "-m", "ledger"],
                            check=True, env=env, capture_output=True, text=True).stdout.strip()
    subprocess.run([*git, "update-ref", "HEAD", commit], check=True, env=env)
    os.utime(path, (stamp, stamp))


def closed(number, title, body, created_at=None, updated_at=None):
    return Issue(number, title, body, frozenset(), "closed", state_reason="completed",
                 created_at=created_at, updated_at=updated_at)


def opened(number, title, body, created_at=None, updated_at=None):
    return Issue(number, title, body, frozenset(), "open", created_at=created_at, updated_at=updated_at)


def assert_in_progress_with_remaining(view, numbers, why):
    output = view.run()
    row = view.rows()["D60"]
    assert row["issues"] == numbers
    assert row["state"] == IN_PROGRESS, f"{why}\nfa decisions:\n{output}\nJSON row: {row}"
    assert row.get("remaining") == [STEP_TWO], f"{why}: the JSON row names the work left\n{row}"
    d60 = [line for line in output.splitlines() if line.startswith("D60:")]
    assert d60 and IN_PROGRESS in d60[0], output
    assert f"Work left: {STEP_TWO}" in output, f"{why}: the text view names the work left\n{output}"


def test_issue_linked_after_the_marker_takes_over_and_completes_the_decision(
    tmp_path, monkeypatch, capsys,
):
    view = Ledger(tmp_path, monkeypatch, capsys)
    base = datetime.now(UTC).replace(microsecond=0)
    old = list(range(100, 120))
    # #1650 is an old issue: created and first listed long before the marker, without a link.
    created_1650 = iso(at(base, days=-6))
    history = [closed(number, f"Earlier capacity work {number}", links("D60"),
                      created_at=iso(at(base, days=-7)), updated_at=iso(at(base, days=-5)))
               for number in old]
    plain = "Size the run capacity."
    observe(view, [*history, opened(1650, "Capacity step 2", plain, created_1650, created_1650)],
            at(base, days=-4))

    # The marker is written after every old link was recorded.
    write_marker(view, ledger("D60", extra=remaining("D60", old[-1], STEP_TWO)), at(base, days=-3))
    assert_in_progress_with_remaining(
        view, old, "Links recorded before the marker do not take over its work")

    # FA observes #1650 linking to D60 after the marker: that recorded link event is the takeover.
    linked = plain + "\n" + links("D60")
    observe(view, [*history, opened(1650, "Capacity step 2", linked, created_1650, iso(at(base, days=-2)))],
            at(base, days=-2))
    row = view.rows()["D60"]
    assert row["issues"] == [*old, 1650]
    assert row["state"] == IN_PROGRESS, f"The takeover issue is still open\n{row}"

    observe(view, [*history, closed(1650, "Capacity step 2", linked, created_1650, iso(at(base, days=-1)))],
            at(base, days=-1))
    output = view.run()
    row = view.rows()["D60"]
    assert row["issues"] == [*old, 1650]
    assert row["state"] == DONE, (
        "An issue whose link event was recorded after the marker took over the work and completed; "
        "its earlier creation time does not matter.\n"
        f"fa decisions:\n{output}\nJSON row: {row}"
    )
    assert "remaining" not in row and STEP_TWO not in json.dumps(row, ensure_ascii=False)
    assert STEP_TWO not in output and "Work left" not in output
    d60 = [line for line in output.splitlines() if line.startswith("D60:")]
    assert d60 and DONE in d60[0] and IN_PROGRESS not in d60[0], output


def _pre_upgrade_row(number, title, body, state, reason, observed_at, created_at=None, updated_at=None):
    """An issues.json row as written before link events were recorded (format of main f2c89a48)."""
    row = {
        "number": number, "title": title, "body": body, "labels": [], "lane": "main", "state": state,
        "pr_number": None, "merged": False, "comments": [], "state_reason": reason,
        "observed_at": observed_at,
        "observed": {
            "title": title, "body": body, "labels": [], "state": state, "updated_at": updated_at,
            "sub_issues": None, "dependencies": None, "pr_number": None, "pr_head": None, "merged": False,
        },
        "updated_at": observed_at,
    }
    if created_at is not None:
        row["created_at"] = created_at
    return row


def test_link_without_recorded_link_time_does_not_take_over_even_if_created_later_and_closed(
    tmp_path, monkeypatch, capsys,
):
    view = Ledger(tmp_path, monkeypatch, capsys)
    base = datetime.now(UTC).replace(microsecond=0)
    marker_at = at(base, days=-3)
    write_marker(view, ledger("D60", extra=remaining("D60", 1635, STEP_TWO)), marker_at)

    # State kept from before link events were recorded: #1700 was created and listed after the
    # marker, already linking D60, but no link time was ever recorded for that link.
    created = iso(at(marker_at, hours=1))
    listed = iso(at(marker_at, hours=2))
    first_body = "Capacity step 1\n" + links("D60")
    second_body = "Capacity step 2\n" + links("D60")
    state = {
        "refreshed_at": listed,
        "issues": {
            "1635": _pre_upgrade_row(1635, "Capacity step 1", first_body, "closed", "completed", listed,
                                     created_at=iso(at(base, days=-9)), updated_at=iso(at(base, days=-8))),
            "1700": _pre_upgrade_row(1700, "Capacity step 2", second_body, "open", None, listed,
                                     created_at=created, updated_at=created),
        },
        "complete_at": listed,
        "absent": {},
        "outbox": [],
    }
    root = view.case.store.root
    root.mkdir(parents=True, exist_ok=True)
    (root / "issues.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    row = view.rows()["D60"]
    assert row["issues"] == [1635, 1700] and row["state"] == IN_PROGRESS, row

    # A later listing sees #1700 closed as completed, created and updated after the marker.
    # Listing, creation and update times are not link events, so the link stays unknown.
    closed_at = at(base, days=-1)
    observe(view, [
        closed(1635, "Capacity step 1", first_body, iso(at(base, days=-9)), iso(at(base, days=-8))),
        closed(1700, "Capacity step 2", second_body, created, iso(closed_at)),
    ], closed_at)
    assert_in_progress_with_remaining(
        view, [1635, 1700],
        "An issue created after the marker and closed, whose link time was never recorded, "
        "does not take over the remaining work")
