"""Order acceptance: a decision stays open until its remaining work has an issue (2026-11-06).

Contract chosen by this lane (marker syntax is the lane's choice; recorded as DECIDE lines):
- A decision that is a rule with nothing to build carries one ledger line in
  docs/decisions.md, anywhere in the file, like a drop record:
      <!-- foragent:decision-rule {"id": "D110"} -->
- An issue that closed with work left carries one ledger line naming the decision, the
  closed issue and what remains:
      <!-- foragent:decision-remaining {"id": "D60", "issue": 1635, "what": "..."} -->
- `fa decisions` (and `--json`) reads only these ledger lines and the recorded issue facts.
  A decision whose linked issues all finished but one of them has remaining work recorded,
  and no other linked issue covers it, is "in progress" and names the remaining text.
  An issue linked to the decision besides the ones carrying a remaining record is the
  issue for the remainder; then the decision follows that issue's state.
Not pinned: the state name a rule decision gets (only that it is not
"decided, nothing ordered") and the JSON field that holds the remaining text.
"""

import json

from foragent.tracker import Issue
from test_order_progress import offline_local_cli
from test_skills_autosync import cli_exit

NOTHING_ORDERED, IN_PROGRESS, DONE = "decided, nothing ordered", "in progress", "done"
STEP_TWO = "Step 2: size the run capacity from the machine at every start"


def ledger(*ids, extra=""):
    return "# Decisions\n\n" + "".join(
        f"### {identity}. Decision {identity} (2026-11-01, Jin)\n- 결정: text {identity}.\n\n"
        for identity in ids) + extra


def rule(identity):
    return "<!-- foragent:decision-rule " + json.dumps({"id": identity}) + " -->\n"


def remaining(identity, issue, what):
    return "<!-- foragent:decision-remaining " + json.dumps(
        {"id": identity, "issue": issue, "what": what}, ensure_ascii=False) + " -->\n"


def links(*ids):
    return "<!-- foragent:decisions " + json.dumps(list(ids)) + " -->"


class Ledger:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.case = offline_local_cli(tmp_path, monkeypatch)
        self.capsys = capsys
        (self.case.root / "docs").mkdir()

    def write(self, text):
        (self.case.root / "docs/decisions.md").write_text(text, encoding="utf-8")

    def issues(self, rows):
        self.case.store.refresh_local_issues(rows, [], {}, self.case.store._now())

    def run(self, *extra):
        self.capsys.readouterr()
        code = cli_exit(self.case.main, ["decisions", *extra, "--repo-root", str(self.case.root)])
        output = self.capsys.readouterr()
        assert code == 0, output.err
        assert self.case.gh_calls == [], "fa decisions reads local facts only"
        return output.out

    def rows(self):
        return {row["id"]: row for row in json.loads(self.run("--json"))}

    def lines(self):
        return {line.split(":", 1)[0]: line for line in self.run().splitlines() if line.startswith("D")}


def test_rule_with_nothing_to_build_is_not_listed_as_nothing_ordered(tmp_path, monkeypatch, capsys):
    view = Ledger(tmp_path, monkeypatch, capsys)
    # D11 shares a prefix with D110; D114 is a second rule; D7 has no record at all.
    view.write(ledger("D7", "D11", "D110", "D114", extra=rule("D110") + "\n" + rule("D114")))
    view.issues([])
    rows = view.rows()
    assert set(rows) == {"D7", "D11", "D110", "D114"}, "A rule decision is still a ledger row"
    for identity in ("D110", "D114"):
        assert rows[identity]["issues"] == []
        assert rows[identity]["state"] != NOTHING_ORDERED, \
            f"{identity} is a rule with nothing to build and must leave the nothing-ordered list"
    for identity in ("D7", "D11"):
        assert rows[identity]["state"] == NOTHING_ORDERED, \
            f"{identity} has no rule record and still waits for an order"
    lines = view.lines()
    assert NOTHING_ORDERED not in lines["D110"] and NOTHING_ORDERED not in lines["D114"]
    assert NOTHING_ORDERED in lines["D7"] and NOTHING_ORDERED in lines["D11"]


def test_rule_record_for_an_unknown_or_other_id_changes_nothing(tmp_path, monkeypatch, capsys):
    view = Ledger(tmp_path, monkeypatch, capsys)
    view.write(ledger("D11", "D12", extra=rule("D110") + rule("D1")))
    view.issues([])
    rows = view.rows()
    assert set(rows) == {"D11", "D12"}, "A record for an id without a heading adds no row"
    assert rows["D11"]["state"] == NOTHING_ORDERED and rows["D12"]["state"] == NOTHING_ORDERED
    view.write(ledger("D11", "D12", extra=rule("D12")))
    rows = view.rows()
    assert rows["D12"]["state"] != NOTHING_ORDERED
    assert rows["D11"]["state"] == NOTHING_ORDERED


def test_only_issue_closed_with_remaining_work_keeps_decision_open_and_names_it(
    tmp_path, monkeypatch, capsys,
):
    view = Ledger(tmp_path, monkeypatch, capsys)
    # #1635 links D60 and D61; only D60 has remaining work recorded. D6 shares a prefix.
    view.write(ledger("D6", "D60", "D61", extra=remaining("D60", 1635, STEP_TWO)))
    view.issues([
        Issue(1635, "Capacity step 1", links("D60", "D61"), frozenset(), "closed",
              state_reason="completed"),
        Issue(1640, "Other", links("D6"), frozenset(), "closed", state_reason="completed"),
    ])
    rows = view.rows()
    assert rows["D60"]["issues"] == [1635]
    assert rows["D60"]["state"] == IN_PROGRESS, "Closed with work left is not done"
    assert STEP_TWO in json.dumps(rows["D60"], ensure_ascii=False), "The row names the remaining work"
    assert rows["D61"]["state"] == DONE, "Remaining work belongs to D60 only"
    assert rows["D6"]["state"] == DONE, "A record for D60 does not touch D6"
    assert STEP_TWO not in json.dumps(rows["D61"], ensure_ascii=False)
    lines = view.run()
    d60 = [line for line in lines.splitlines() if line.startswith("D60:")]
    assert d60 and DONE + " " not in d60[0] and IN_PROGRESS in d60[0]
    assert STEP_TWO in lines, "The text view names the remaining work too"
    assert lines.count(STEP_TWO) == 1, "Named once, under D60 only"


def test_linked_remainder_issue_decides_the_state(tmp_path, monkeypatch, capsys):
    view = Ledger(tmp_path, monkeypatch, capsys)
    view.write(ledger("D60", extra=remaining("D60", 1635, STEP_TWO)))
    first = Issue(1635, "Capacity step 1", links("D60"), frozenset(), "closed", state_reason="completed")
    view.issues([first])
    rows = view.rows()
    assert rows["D60"]["state"] == IN_PROGRESS, "Before a remainder issue the work left keeps it open"

    def remainder(state, reason=None):
        return Issue(1700, "Capacity step 2", links("D60"), frozenset(), state, state_reason=reason)

    view.issues([first, remainder("open")])
    row = view.rows()["D60"]
    assert row["issues"] == [1635, 1700] and row["state"] == IN_PROGRESS
    view.issues([first, remainder("closed", "not_planned")])
    assert view.rows()["D60"]["state"] == IN_PROGRESS, "A cancelled remainder does not finish it"
    view.issues([first, remainder("closed", "completed")])
    row = view.rows()["D60"]
    assert row["state"] == DONE, "The remainder issue finished, so the decision is done"
    assert STEP_TWO not in json.dumps(row, ensure_ascii=False), "Covered work is no longer named as left"
    view.issues([first, remainder("open")])
    assert view.rows()["D60"]["state"] == IN_PROGRESS, "Reopening the remainder reopens the decision"
    # The remainder issue can itself close with work left: the same rule applies again.
    view.write(ledger("D60", extra=remaining("D60", 1635, STEP_TWO)
                      + remaining("D60", 1700, "Step 3: show the capacity in fa status")))
    view.issues([first, remainder("closed", "completed")])
    row = view.rows()["D60"]
    assert row["state"] == IN_PROGRESS
    assert "Step 3: show the capacity in fa status" in json.dumps(row, ensure_ascii=False)


def test_all_linked_issues_closed_without_remaining_work_is_done_beside_one_with_work_left(
    tmp_path, monkeypatch, capsys,
):
    view = Ledger(tmp_path, monkeypatch, capsys)
    view.write(ledger("D60", "D71", "D79", extra=remaining("D60", 1635, STEP_TWO)))
    view.issues([
        Issue(1635, "Capacity step 1", links("D60"), frozenset(), "closed", state_reason="completed"),
        Issue(11, "First", links("D79"), frozenset(), "closed", state_reason="completed"),
        Issue(12, "Second", links("D79"), frozenset(), "open"),
        Issue(13, "Only", links("D71"), frozenset(), "closed", state_reason="completed"),
    ])
    view.case.store.update_local_issue(12, state="closed", state_reason=None, merged=True)
    rows = view.rows()
    # Regression: no remaining work recorded keeps today's derivation.
    assert rows["D71"]["issues"] == [13] and rows["D71"]["state"] == DONE
    assert rows["D79"]["issues"] == [11, 12] and rows["D79"]["state"] == DONE
    for identity in ("D71", "D79"):
        assert STEP_TWO not in json.dumps(rows[identity], ensure_ascii=False)
    # In the same ledger, the decision with work left is not done.
    assert rows["D60"]["state"] == IN_PROGRESS
    assert STEP_TWO in json.dumps(rows["D60"], ensure_ascii=False)
    # And without the record, the same facts read as done again (as today).
    view.write(ledger("D60", "D71", "D79"))
    rows = view.rows()
    assert {identity: rows[identity]["state"] for identity in rows} == {
        "D60": DONE, "D71": DONE, "D79": DONE}
