"""Acceptance test for eta-remaining-work. Locked by Foragent; do not edit."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import re

import pytest

from test_order_progress import isolated_home  # noqa: F401
from test_squirrel_running import plain_frame, view_offline  # noqa: F401
from test_view_status import status_case


@pytest.fixture
def eta_case(git_history, tmp_path, monkeypatch, view_offline):
    """Reuse the status case's fake clock/tracker and real local run records."""
    from foragent import capacity

    case, _ = status_case(git_history, tmp_path, monkeypatch)
    case.clock[0] = datetime(2026, 10, 25, tzinfo=UTC)
    monkeypatch.setattr(capacity, "machine_root", lambda: case.store.root.parent)
    # The configured ceiling is deliberately different from today's budget.
    case.config = case.ctx.config = replace(case.config, max_parallel=8)
    capacity.write_state(case.store, 2, case.clock[0])
    case.tracker.add_issue(900, "Finish the remaining work")
    case.tracker.set_parent(1, 900)
    case.tracker.add_labels(1, ["size:l"])
    case.store.update_run(1, started_at=(case.clock[0] - timedelta(minutes=10)).isoformat(),
                          last_progress_at=case.clock[0].isoformat(),
                          phase_started_at=case.clock[0].isoformat(),
                          last_activity_at=case.clock[0].isoformat())
    # The other status-case issues stay approved but outside this order.
    return case


def add_work(case, number, size, *, approved=True):
    from foragent.issue import body_hash

    body = f"Work for issue {number}"
    labels = [f"size:{size}", "lane:api", *(["approved"] if approved else [])]
    case.tracker.add_issue(number, f"Work {number}", body, labels=labels)
    case.tracker.set_parent(number, 900)
    if approved:
        case.store.save_approval(number, body=body, body_sha=body_hash(body),
                                 acceptance_blobs={}, approved_by="human",
                                 approved_at=case.clock[0].isoformat())


def merged_run(case, number, size, duration, *, lane="api", age=timedelta(minutes=20)):
    now = case.clock[0]
    case.clock[0] = now - age - duration
    add_work(case, number, size)
    # Execution tier is independent of the issue's size label.
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number), lane=lane, tier="m")
    case.clock[0] = now - age
    case.store.update_run(number, last_progress_at=case.clock[0].isoformat())
    case.store.finish_run(number, outcome="merged")
    case.tracker.close_issue(number)
    case.clock[0] = now


def outside_merge(case, number, duration, *, approved=True, recorded=True):
    """A real fake-tracker merge, without manufacturing a gate 'merged' outcome."""
    now = case.clock[0]
    case.clock[0] = now - duration
    add_work(case, number, "l", approved=approved)
    if recorded:
        case.store.start_run(number, branch=f"foragent/issue-{number}",
                             worktree=case.store.worktree_path(number), lane="api")
    pr = case.tracker.create_pr(head=f"foragent/issue-{number}", base="main",
                                title=f"Outside work {number}", body=f"Closes #{number}")
    case.clock[0] = now
    case.tracker.merge_pr(pr, expected_head_sha=case.tracker.pr_head_sha(pr))
    case.tracker.close_issue(number)


def snapshot(case):
    from foragent import status, watch
    from foragent.state import StateStore

    tracker = case.tracker
    issues = tracker.list_issues("all")
    case.store.refresh_local_issues(
        issues, tracker.created_prs, case.config.lanes, case.clock[0],
        parents={issue.number: parent for issue in issues
                 if (parent := tracker.parent_of(issue.number)) is not None},
        complete_prs=True,
    )
    # A fresh reader must learn from persisted machine records, not a writer's cache.
    reader = StateStore(case.store.root, now=lambda: case.clock[0])
    payload = status.collect(reader, case.config, case.clock[0])
    return watch.collect(payload, tracker, reader, case.config, case.clock[0])


def progress_words(case, value, *, language="en"):
    from foragent.watch_sections import progress

    return "\n".join("".join(words for words, _ in line) for line in progress.render(
        value, replace(case.config, language=language), case.root, now=case.clock[0]))


def assert_basis(words, size, source):
    # Keep wording/units free while associating the source with its own size.
    entries = re.split(r"(?=size:[sml]\b)", words)
    entry = next((part.splitlines()[0] for part in entries if part.startswith(f"size:{size}")), "")
    pattern = rf"(?<!\d){source}(?!\d)" if isinstance(source, int) else rf"\b{source}\b"
    assert re.search(pattern, entry), f"size:{size} must show basis {source!r}: {words}"


def test_eta_size_weighted_sum_ignores_ten_trivial_merges_and_uses_current_budget(eta_case):
    from foragent import capacity

    case = eta_case
    add_work(case, 10, "l")
    add_work(case, 11, "m")
    add_work(case, 12, "l", approved=False)
    for size, hours, first in (("l", (4, 8, 24), 100), ("m", (2, 4, 18), 200)):
        for number, duration in enumerate(hours, first):
            merged_run(case, number, size, timedelta(hours=duration))
    before = snapshot(case)
    for number in range(300, 310):
        merged_run(case, number, "s", timedelta(minutes=1), lane="docs")
    after = snapshot(case)
    expected = case.clock[0] + timedelta(hours=(2 * 8 + 4) / 2)
    assert before.eta == after.eta == expected, (
        "ETA must sum size medians for approved open children / current budget; "
        f"ten trivial merges changed {before.eta} to {after.eta}, expected {expected}"
    )
    capacity.write_state(case.store, 4, case.clock[0])
    assert snapshot(case).eta == case.clock[0] + timedelta(hours=(2 * 8 + 4) / 4), (
        "The current parallel budget, rather than the configured ceiling or active count, divides the work"
    )


def test_eta_shows_date_range_and_per_size_sample_basis_through_messages(eta_case, monkeypatch):
    from foragent import i18n, watch
    from foragent.watch_sections import progress

    case = eta_case
    for number in (10, 11, 12):
        add_work(case, number, "l")
    add_work(case, 13, "m")
    add_work(case, 14, "s")
    for size, hours, first in (("s", (1, 2, 3), 100), ("m", (2, 4, 8, 10), 200),
                               ("l", (12, 24, 36, 48, 72), 300)):
        for number, duration in enumerate(hours, first):
            merged_run(case, number, size, timedelta(hours=duration))
    value = snapshot(case)
    for language in ("en", "ko"):
        looked_up = []

        def recorded_text(key, language="en", **values):
            result = i18n.text(key, language, **values)
            looked_up.append(result)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(progress, "text", recorded_text)
            words = progress_words(case, value, language=language)
        frame = plain_frame(watch.render_frame(value, replace(case.config, language=language),
                                               case.root, width=240, now=case.clock[0]))
        # Linearly interpolated IQR sums / 2 span Oct 27-29 in UTC;
        # medians sum to 76 hours, Oct 28. Match the view's local dates.
        # Date-only assertions leave clock precision and range punctuation free.
        for hours in ((4 * 24 + 3.5 + 1.5) / 2, (4 * 48 + 8.5 + 2.5) / 2):
            local = (case.clock[0] + timedelta(hours=hours)).astimezone()
            date = i18n.text("progress_on_day", language, month=local.month, day=local.day, time="").strip()
            assert date in words, f"ETA must show both spread endpoints ({date}): {words}"
            assert date in frame and date in "\n".join(looked_up), "The visible range must use the messages registry"
        for size, count in (("s", 3), ("m", 4), ("l", 5)):
            assert_basis(words, size, count)
            assert_basis(frame, size, count)
            assert_basis(" ".join(looked_up), size, count)
    assert value.eta == case.clock[0] + timedelta(hours=(4 * 36 + 6 + 2) / 2)


def test_eta_defaults_remain_until_three_merged_runs_of_each_size(eta_case):
    case = eta_case
    add_work(case, 10, "s")
    add_work(case, 11, "m")
    baseline = snapshot(case)
    assert baseline.eta is not None and baseline.eta > case.clock[0], (
        "Every remaining size needs a finite default ETA before any merges exist"
    )
    for count in range(3):
        if count:
            for size, hours, first in (("s", 12, 100), ("m", 24, 200), ("l", 72, 300)):
                merged_run(case, first + count, size, timedelta(hours=hours))
        value = snapshot(case)
        assert value.eta == baseline.eta, "One or two runs of a size must not replace its default"
        words = progress_words(case, value)
        for size in ("s", "m", "l"):
            assert_basis(words, size, "default")
    for size, hours, number in (("s", 12, 103), ("m", 24, 203), ("l", 72, 303)):
        merged_run(case, number, size, timedelta(hours=hours))
    learned = snapshot(case)
    assert learned.eta == case.clock[0] + timedelta(hours=(12 + 24 + 72) / 2), (
        "The third merged run must switch each size to its learned median"
    )
    for size in ("s", "m", "l"):
        assert_basis(progress_words(case, learned), size, 3)


def test_eta_three_hours_without_merges_keeps_estimate_beside_running_issues_and_phases(eta_case):
    from foragent.i18n import text

    case = eta_case
    add_work(case, 10, "m")
    case.store.start_run(10, branch="foragent/issue-10", worktree=case.store.worktree_path(10), lane="web")
    case.store.update_run(10, phase="implementing", phase_started_at=case.clock[0].isoformat())
    for size, hours, first in (("l", 8, 100), ("m", 4, 200)):
        for number in range(first, first + 3):
            merged_run(case, number, size, timedelta(hours=hours))
    before = snapshot(case)
    case.clock[0] += timedelta(hours=3)
    after = snapshot(case)
    assert after.eta == case.clock[0] + timedelta(hours=(8 + 4) / 2), (
        "Three hours without merges must retain the estimate from the remaining work"
    )
    assert before.eta == after.eta - timedelta(hours=3)
    assert after.recent == 0
    words = progress_words(case, after)
    assert text("progress_no_evidence", "en", count=0, remaining=2) in words
    for number, phase in ((1, "reviewing"), (10, "implementing")):
        assert any(f"#{number}" in line and text(phase, "en") in line for line in words.splitlines()), (
            f"The estimate section must show running #{number} and its {phase} phase: {words}"
        )
    assert text("progress_unknown_finish", "en") not in words


def test_eta_outside_merge_counts_only_approved_issues_with_recorded_runs(eta_case):
    case = eta_case
    merged_run(case, 100, "l", timedelta(hours=4))
    merged_run(case, 101, "l", timedelta(hours=8))
    outside_merge(case, 102, timedelta(hours=24))
    outside_merge(case, 103, timedelta(hours=100), approved=False)
    outside_merge(case, 104, timedelta(hours=100), recorded=False)
    # Neither negative control nor the outside completion has a gate merge entry.
    assert {run.number for run in case.store.read_history() if run.outcome == "merged"} == {100, 101}
    for _ in range(2):
        value = snapshot(case)
        assert value.recent == 3, (
            "The outside merge closing an approved issue with a run must count once; "
            f"unapproved/no-run merges must not count (got {value.recent})"
        )
        assert value.eta == case.clock[0] + timedelta(hours=8 / 2), (
            "Start-to-merge durations 4h, 8h, 24h must train size:l's median, including the outside merge"
        )
        assert_basis(progress_words(case, value), "l", 3)


def test_eta_no_remaining_approved_issues_has_no_estimate_line(eta_case):
    from foragent.i18n import text

    case = eta_case
    for number in range(100, 103):
        merged_run(case, number, "l", timedelta(hours=8))
    case.store.finish_run(1, outcome="merged")
    case.tracker.close_issue(1)
    # An open draft child must not manufacture remaining approved work.
    add_work(case, 10, "l", approved=False)
    value = snapshot(case)
    assert value.eta is None, "No approved open children means there is no finish estimate"
    words = progress_words(case, value)
    assert text("progress_unknown_finish", "en") not in words
    assert "done around" not in words
