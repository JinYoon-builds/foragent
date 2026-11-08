"""Old completed links cannot take over a decision's newly recorded remaining work."""

from foragent.tracker import Issue
from test_decision_remaining_work import DONE, IN_PROGRESS, STEP_TWO, Ledger, ledger, links, remaining


def test_remaining_marker_shows_in_progress_when_only_old_closed_issues_are_linked(
    tmp_path, monkeypatch, capsys,
):
    view = Ledger(tmp_path, monkeypatch, capsys)
    numbers = list(range(100, 120))
    view.write(ledger("D60"))
    view.issues([
        Issue(number, f"Earlier capacity work {number}", links("D60"), frozenset(),
              "closed", state_reason="completed")
        for number in numbers
    ])
    before = view.rows()["D60"]
    assert before["issues"] == numbers
    assert before["state"] == DONE

    # Every link and completed closure predates this marker; no takeover issue is added.
    view.write(ledger("D60", extra=remaining("D60", numbers[-1], STEP_TWO)))
    output = view.run()
    row = view.rows()["D60"]

    assert row["issues"] == numbers
    assert row["state"] == IN_PROGRESS, (
        f"Old completed links cannot cover newly recorded remaining work.\n"
        f"fa decisions:\n{output}\nJSON row: {row}"
    )
    assert row["remaining"] == [STEP_TWO]
    assert IN_PROGRESS in output.splitlines()[0]
    assert f"Work left: {STEP_TWO}" in output
