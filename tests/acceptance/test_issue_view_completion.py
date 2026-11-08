"""Acceptance test for n79-merged-issue-state. Locked by Foragent; do not edit.

`fa dashboard <N> --once` must say what a closed issue's recorded completion is (merged when
the gate recorded its PR merged, else closed) and name the associated PR when one is recorded,
whether or not the issue has a run record. Before this issue a closed issue without a run
printed only its title, a finished run hid the PR, and a stale unfinished run on a merged issue
was presented as current work ('writing code').

Real CLI, gate tick and local state of the shared status world; only GitHub and the clock are
fake. PR identity is `#<pr>` or a `/pull/<pr>` link; current state is the part of the view above
the first line that labels history (the locked issue-view-current-state rule).
"""

from datetime import timedelta
import re

import pytest

from test_issue_view_current_state import (HANDED_WORDS, HISTORY_LABEL, MERGED, MERGED_WORDS, _current,
                                           _merged, _run, _tick, _world)

NO_RUN = 7
CLOSED = 8
STALE = 9

CLOSED_WORDS = {
    "en": re.compile(r"\bclosed\b", re.I),
    "ko": re.compile(r"닫혔|닫혀|닫힌|닫았"),
}
# Negated merge phrases are not a claim of a merge.
NOT_MERGED = {
    "en": re.compile(r"\b(not|never|wasn't|was not|isn't|is not)\s+(been\s+)?merged\b|\bunmerged\b"
                     r"|\bwithout (a |any |being )?merg\w*|\bno merge\b", re.I),
    "ko": re.compile(r"병합되지 않|병합 안|병합하지 않|병합 기록이 없|병합 없이"),
}
# The words a running 'implementing' step shows (the stale phase).
STALE_PHASE = {
    "en": re.compile(r"writing code|in this step", re.I),
    "ko": re.compile(r"코드 쓰는 중|이 단계를"),
}


def _show(main, capsys, number):
    capsys.readouterr()
    code = _run(main, ["dashboard", str(number), "--once"])
    captured = capsys.readouterr()
    assert code == 0, f"`fa dashboard {number} --once` must exit 0 for a known issue: {captured.err}"
    assert f"#{number}" in captured.out
    return captured.out


def _pr_lines(out, pr):
    return [line for line in out.splitlines() if re.search(rf"#{pr}(?!\d)|/pull/{pr}(?!\d)", line)]


def _pr_shown(out, pr):
    """The PR is named, on one line of the view (not repeated)."""
    return len(_pr_lines(out, pr)) == 1


def _affirms_merged(out, language):
    return MERGED_WORDS[language].search(NOT_MERGED[language].sub(" ", out))


def _row(case, number):
    return next((row for row in case.store.local_issues() if row.get("number") == number), None)


def _no_run(case, number):
    assert case.store.get_run(number) is None, f"Setup: #{number} has no run record"
    assert not any(run.number == number for run in case.store.read_history()), (
        f"Setup: #{number} has no run history")


def _add_open(case, number, title):
    case.tracker.add_issue(number, title, case.tracker.get_issue(1).body, labels=["approved", "lane:docs"])


def _merge_and_close(case, number):
    pr = case.tracker.create_pr(head=f"foragent/issue-{number}", base="main", title=f"Work for #{number}",
                                body=f"Closes #{number}")
    case.clock[0] += timedelta(minutes=2)
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.tracker.close_issue(number, reason="completed")
    return pr


@pytest.mark.parametrize("language", ["en", "ko"])
def test_merged_issue_with_finished_run_shows_merged_and_its_pr(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    pr = _merged(case)  # last run handed the PR to the merge owner, which merged it
    _tick(case)
    row = _row(case, MERGED)
    assert row and row.get("state") == "closed" and row.get("merged"), f"Setup: merge recorded: {row}"
    assert pr != MERGED
    assert any(run.number == MERGED and run.status != "running"
               for run in [*filter(None, [case.store.get_run(MERGED)]), *case.store.read_history()]), (
        "Setup: a finished run exists")

    out = _show(main, capsys, MERGED)
    merged = MERGED_WORDS[language].search(_current(out, language))
    assert merged, f"#{MERGED}'s merged state leads the view, above its run history:\n{out}"
    handed = HANDED_WORDS[language].search(out)
    assert handed is None or handed.start() > merged.start(), out
    assert _pr_shown(out, pr), f"The merged issue's PR #{pr} must be shown once:\n{out}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_merged_issue_without_run_record_shows_merged_and_its_pr(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _add_open(case, NO_RUN, "Tidy the page")
    _tick(case)
    pr = _merge_and_close(case, NO_RUN)
    _tick(case)
    row = _row(case, NO_RUN)
    assert row and row.get("state") == "closed" and row.get("merged") and row.get("pr_number") == pr, (
        f"Setup: the gate recorded #{NO_RUN} closed with PR #{pr} merged: {row}")
    _no_run(case, NO_RUN)
    assert pr != NO_RUN

    out = _show(main, capsys, NO_RUN)
    assert _affirms_merged(_current(out, language), language), (
        f"Without any run record, #{NO_RUN} must still read merged:\n{out}")
    assert _pr_shown(out, pr), f"The merged issue's PR #{pr} must be shown once without a run record:\n{out}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_completed_issue_with_stale_unfinished_run_leads_with_completion(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _add_open(case, STALE, "Stale run")
    _tick(case)
    pr = _merge_and_close(case, STALE)
    _tick(case)
    # A run record still says it is writing code although the issue is merged and closed.
    case.store.start_run(STALE, branch=f"foragent/issue-{STALE}", worktree=case.store.worktree_path(STALE),
                         lane="docs")
    case.store.update_run(STALE, phase="implementing", phase_started_at=case.clock[0].isoformat())
    assert case.store.get_run(STALE).status == "running", "Setup: a stale unfinished run is recorded"
    row = _row(case, STALE)
    assert row and row.get("state") == "closed" and row.get("merged"), f"Setup: merge recorded: {row}"

    out = _show(main, capsys, STALE)
    now = _current(out, language)
    merged = MERGED_WORDS[language].search(now)
    assert merged, f"#{STALE} is merged; the view must lead with that, above any history:\n{out}"
    assert not STALE_PHASE[language].search(now), (
        f"The stale 'implementing' step must not be presented as current work:\n{out}")
    phase = STALE_PHASE[language].search(out)
    assert phase is None or phase.start() > merged.start(), out
    assert _pr_shown(out, pr), f"The merged issue's PR #{pr} must be shown once:\n{out}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_closed_issue_without_pr_says_closed_and_names_no_pr(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _add_open(case, CLOSED, "Old idea")
    _tick(case)
    case.tracker.close_issue(CLOSED, reason="not_planned")
    _tick(case)
    row = _row(case, CLOSED)
    assert row and row.get("state") == "closed" and not row.get("merged") and not row.get("pr_number"), (
        f"Setup: #{CLOSED} closed with no PR: {row}")
    _no_run(case, CLOSED)

    out = _show(main, capsys, CLOSED)
    now = _current(out, language)
    assert CLOSED_WORDS[language].search(now), f"#{CLOSED} must read closed:\n{out}"
    assert not _affirms_merged(out, language), f"Closure alone is not a merge:\n{out}"
    others = {int(n) for n in re.findall(r"#(\d+)", out)} - {CLOSED}
    assert not others and "/pull/" not in out, f"No PR is recorded, so none is shown once:\n{out}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_closed_issue_with_unmerged_pr_says_closed_and_shows_that_pr(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _add_open(case, CLOSED, "Old idea")
    _tick(case)
    pr = case.tracker.create_pr(head=f"foragent/issue-{CLOSED}", base="main", title="Old idea",
                                body=f"Closes #{CLOSED}")
    case.clock[0] += timedelta(minutes=2)
    case.tracker.close_issue(CLOSED, reason="not_planned")
    _tick(case)
    row = _row(case, CLOSED)
    assert row and row.get("state") == "closed" and not row.get("merged") and row.get("pr_number") == pr, (
        f"Setup: #{CLOSED} closed while its PR #{pr} stayed unmerged: {row}")
    _no_run(case, CLOSED)

    out = _show(main, capsys, CLOSED)
    now = _current(out, language)
    assert CLOSED_WORDS[language].search(now), f"#{CLOSED} must read closed:\n{out}"
    assert not _affirms_merged(out, language), f"An unmerged PR is not a merge:\n{out}"
    assert _pr_shown(out, pr), f"The recorded PR #{pr} is shown once:\n{out}"


@pytest.mark.parametrize("language", ["en", "ko"])
def test_closed_issue_with_finished_run_and_unmerged_pr_says_closed_and_shows_that_pr(
    git_history, tmp_path, monkeypatch, capsys, language,
):
    case, main = _world(git_history, tmp_path, monkeypatch, language)
    _add_open(case, CLOSED, "Old idea")
    _tick(case)
    case.store.start_run(CLOSED, branch=f"foragent/issue-{CLOSED}", worktree=case.store.worktree_path(CLOSED),
                         lane="docs")
    pr = case.tracker.create_pr(head=f"foragent/issue-{CLOSED}", base="main", title="Old idea",
                                body=f"Closes #{CLOSED}")
    case.clock[0] += timedelta(minutes=2)
    case.tracker.close_issue(CLOSED, reason="not_planned")
    _tick(case)  # the gate stops the run of an issue closed outside it
    run = case.store.get_run(CLOSED)
    assert run is not None and run.status != "running", f"Setup: the run finished: {run}"
    row = _row(case, CLOSED)
    assert row and row.get("state") == "closed" and not row.get("merged") and row.get("pr_number") == pr, (
        f"Setup: #{CLOSED} closed while its PR #{pr} stayed unmerged: {row}")
    assert pr != CLOSED

    out = _show(main, capsys, CLOSED)
    assert CLOSED_WORDS[language].search(_current(out, language)), f"#{CLOSED} must read closed:\n{out}"
    assert not _affirms_merged(out, language), f"Closure with an unmerged PR is not a merge:\n{out}"
    assert _pr_shown(out, pr), f"With finished-run history, the recorded PR #{pr} is shown once:\n{out}"
