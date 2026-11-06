"""A skipped check's creation timestamp must not consume the CI running wait.

Architect r875 (2026-11-06): acceptance-base is skipped on pull_request, but
GitHub stamps its startedAt at creation while the other jobs are still queued.
Reuse the queue-wait recording, real publication flow and GhTracker readers.
"""

import json

import pytest

from test_ci_queue_wait import (
    LIMIT, REPO, QueuedCI, assert_ci_hold, held_after, stamp,
)
from test_ci_queue_wait import (  # noqa: F401 -- reuse the recording and isolation fixtures
    ci_evidence_recording, isolated_home, no_external_execution,
    offline_acceptance, queued_case, run,
)


class SkippedRowCI(QueuedCI):
    """Queued/running jobs plus an acceptance-base check skipped at creation."""

    def check_rows(self):
        rows = super().check_rows()
        created = stamp(self.first_read)
        rows.append({
            "bucket": "skipping", "state": "SKIPPED", "name": "acceptance-base",
            "workflow": "foragent-acceptance",
            "link": f"https://github.com/{REPO}/actions/runs/1/job/{len(rows) + 1}",
            "startedAt": created, "completedAt": created,
            "event": "pull_request", "description": "",
        })
        # Keep a pending job first: QueuedCI uses it for gh's pending exit 8.
        return rows

    def command(self, args, input=None):
        output = super().command(args, input)
        if args[:1] == ["api"] and any(
            arg.startswith((f"repos/{REPO}/commits/", f"https://api.github.com/repos/{REPO}/commits/"))
            and arg.endswith("/check-runs") for arg in args
        ):
            # QueuedCI maps every check to the current phase. The skipped job
            # is already completed in the REST answer throughout all phases.
            response = json.loads(output)
            checks = response if isinstance(response, list) else response["check_runs"]
            skipped = next(check for check in checks if check["name"] == "acceptance-base")
            skipped.update(status="completed", conclusion="skipped",
                           started_at=stamp(self.first_read), completed_at=stamp(self.first_read))
            return json.dumps(response)
        return output


class StaggeredCI(QueuedCI):
    """The first job starts immediately; the others leave the queue later."""

    def check_rows(self):
        rows = super().check_rows()
        rows[0]["startedAt"] = stamp(self.first_read)
        if rows[0]["state"] == "QUEUED":
            rows[0]["state"] = "IN_PROGRESS"
        return rows

    def command(self, args, input=None):
        output = super().command(args, input)
        if args[:1] == ["api"] and any(
            arg.startswith((f"repos/{REPO}/commits/", f"https://api.github.com/repos/{REPO}/commits/"))
            and arg.endswith("/check-runs") for arg in args
        ):
            response = json.loads(output)
            checks = response if isinstance(response, list) else response["check_runs"]
            # QueuedCI gives every job the later start. Match the first job's
            # REST status and timestamp to its independently running check row.
            first = next(check for check in checks if check["name"] == "test (1)")
            first.update(status="completed" if self.phase() == "completed" else "in_progress",
                         started_at=stamp(self.first_read))
            return json.dumps(response)
        return output


@pytest.fixture
def skipped_case(queued_case, monkeypatch):
    def build(*, queued, running):
        case = queued_case(queued=queued, running=running)
        # Reuse the fixture's project budget and bounded fake clock as well.
        case.ci = SkippedRowCI(case, monkeypatch, queued=queued, running=running)
        return case

    return build


def test_skipped_row_at_creation_does_not_start_the_wait(skipped_case):
    queued, running = 15 * 60, 15 * 60
    case = skipped_case(queued=queued, running=running)

    outcome = run(case)

    assert not case.ci.runaway
    assert case.store.load_flag(1) is None, (
        "SKIPPED acceptance-base at creation plus 15 min queued and 15 min running "
        f"must not be held (outcome: {outcome!r}, flag: {case.store.load_flag(1)})"
    )
    assert outcome == "merged" and case.tracker.merged_prs == [2], (
        f"All-green CI within the running wait must merge, got {outcome!r}"
    )
    assert held_after(case) >= queued + running, "It merged before CI finished"
    assert len(case.implementer.calls) == 1, "Waiting for CI must not cost a worker turn"


def test_wait_starts_when_the_last_check_leaves_the_queue(queued_case, monkeypatch):
    queued, running = 15 * 60, 15 * 60
    case = queued_case(queued=queued, running=running)
    case.ci = StaggeredCI(case, monkeypatch, queued=queued, running=running)

    outcome = run(case)

    assert not case.ci.runaway
    assert case.store.load_flag(1) is None, (
        "One job starting immediately must not consume the CI wait while the other jobs "
        "are queued for 15 min, then run for 15 min "
        f"(outcome: {outcome!r}, flag: {case.store.load_flag(1)})"
    )
    assert outcome == "merged" and case.tracker.merged_prs == [2], (
        f"All-green CI within the wait from the last queued check must merge, got {outcome!r}"
    )
    assert held_after(case) >= queued + running, "It merged before CI finished"
    assert len(case.implementer.calls) == 1, "Waiting for CI must not cost a worker turn"


def test_running_past_the_limit_after_the_queue_is_still_held(skipped_case):
    queued = 5 * 60
    case = skipped_case(queued=queued, running=25 * 60)

    outcome = run(case)

    assert_ci_hold(case, outcome)
    # The wait counts from the last check leaving the queue, not from the skipped row's creation stamp.
    assert queued + LIMIT - 60 <= held_after(case) <= queued + LIMIT + 60, (
        f"Hold must come about {LIMIT}s after the queue ended ({queued}s), got {held_after(case)}s")
    assert any("CI ran past the wait limit." in comment.body
               for comment in case.tracker.list_comments(1)), (
        "Running CI must retain the existing timeout explanation"
    )
