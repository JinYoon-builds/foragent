"""Acceptance test for ci-queue-wait: time queued on GitHub does not use the CI wait.

Measured 2026-11-04 ~04:50Z: 11 Actions runs queued and 4 in progress; a ci run
took 10-24 min and foragent-acceptance 13-24 min. The gate's CI wait counts from
the moment the PR is ready, so a PR whose runs sat in GitHub's queue was held as
"CI did not finish" (timeout) while its CI later went all green: #1653, #1472,
#1655, #1836 and #1899 that day, each resolved by hand.

Now the wait counts only the time CI is actually running. A run that is still
running past the wait's limit is held as today. A run that never leaves the
queue still ends, held, after a much longer ceiling.

Real runner, publish flow, checks: ci comparison and GhTracker readers (the
existing ci-mode recording: real Git, real artifacts, FakeTracker writes). Only
GitHub's answers and time are fake. GitHub reports the queue through the fields
it already has: `gh pr checks` rows (bucket "pending" with state QUEUED or
IN_PROGRESS and a zero startedAt while queued) and Actions run / check run
`status` ("queued", "in_progress", "completed"). Every `pr_*` read goes through
GhTracker on that recording, so the lane may read the queue from either source.
"""

from datetime import timedelta
import json
import subprocess
from urllib.parse import urlsplit

import pytest

from foragent.reasons import Reason
from test_ci_mode import (  # noqa: F401 -- the existing ci-mode recording and its guards
    REPO, ComparisonCI, ci_evidence_recording, mode_case, no_external_execution, run,
)
from test_skills_autosync import isolated_home  # noqa: F401
from test_state_split import offline_acceptance  # noqa: F401

LIMIT = 20 * 60  # Today's CI wait: 40 polls x 30 s, at most 1200 s from its start.
QUEUED = 30 * 60  # Longer than the whole CI wait: the 2026-11-04 queue.
RUNNING = 10 * 60  # Inside the limit once the runs start.
RUNAWAY = 30 * 60 * 60  # Fake time after which the test gives up on a wait that never ends.
ZERO = "0001-01-01T00:00:00Z"  # gh prints this startedAt/completedAt for a check that has not started.


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class QueuedCI(ComparisonCI):
    """GitHub whose runs for the PR head sit queued, then run, then finish green.

    The clock starts at the first CI read for the published head. `queued` and
    `running` are seconds; None means "for ever".
    """

    def __init__(self, case, monkeypatch, *, queued, running):
        super().__init__(case, monkeypatch)
        self.queued, self.running = queued, running
        self.first_read = None
        self.runaway = False
        self.legacy.passing = True

    def phase(self):
        now = self.case.clock[0]
        if self.first_read is None:
            self.first_read = now
        elapsed = (now - self.first_read).total_seconds()
        if self.queued is None or elapsed < self.queued:
            return "queued"
        if self.running is None or elapsed < self.queued + self.running:
            return "in_progress"
        return "completed"

    def started_at(self):
        return self.first_read + timedelta(seconds=self.queued)

    def overlay(self, run):
        phase = self.phase()
        run = {**run, "status": phase, "conclusion": "success" if phase == "completed" else None}
        created = stamp(self.first_read)
        # GitHub sets run_started_at when the run is created, so it does not tell queued from running.
        run.update(created_at=created, run_started_at=created,
                   updated_at=stamp(self.case.clock[0]))
        return run

    def measurement_run(self):
        return self.overlay(super().measurement_run())

    def runs_by_head(self):
        response = super().runs_by_head()
        response["workflow_runs"] = [self.overlay(item) for item in response["workflow_runs"]]
        return response

    def check_rows(self):
        phase = self.phase()
        started = ZERO if phase == "queued" else stamp(self.started_at())
        completed = (stamp(self.started_at() + timedelta(seconds=self.running))
                     if phase == "completed" else ZERO)
        state = {"queued": "QUEUED", "in_progress": "IN_PROGRESS", "completed": "SUCCESS"}[phase]
        bucket = "pass" if phase == "completed" else "pending"
        rows = []
        for workflow, name in (("ci", "test (1)"), ("ci", "test (2)"), ("foragent-acceptance", "acceptance (0)"),
                               ("foragent-acceptance", "acceptance (1)")):
            rows.append({"bucket": bucket, "state": state, "name": name, "workflow": workflow,
                         "link": f"https://github.com/{REPO}/actions/runs/1/job/{len(rows) + 1}",
                         "startedAt": started, "completedAt": completed, "event": "pull_request",
                         "description": ""})
        return rows

    def command(self, args, input=None):
        if args[:2] == ["pr", "checks"]:
            rows = self.check_rows()
            fields = args[args.index("--json") + 1].split(",") if "--json" in args else list(rows[0])
            output = json.dumps([{field: row[field] for field in fields} for row in rows])
            if rows[0]["bucket"] == "pending":
                # Real gh exits 8 for pending checks, with the listing on stdout.
                raise subprocess.CalledProcessError(8, ["gh", *args], output=output, stderr="")
            return output
        if args[:1] == ["api"]:
            endpoint = next((arg for arg in args if arg.startswith((f"repos/{REPO}/", "https://api.github.com/"))), "")
            path = urlsplit(endpoint).path.removeprefix("/")
            if path.startswith(f"repos/{REPO}/commits/") and path.endswith("/check-runs"):
                phase = self.phase()
                checks = [{"id": 8200 + index, "name": row["name"], "head_sha": self.case.tracker.pr_head_sha(2),
                           "status": phase, "conclusion": "success" if phase == "completed" else None,
                           "started_at": None if phase == "queued" else row["startedAt"],
                           "completed_at": None if phase != "completed" else row["completedAt"],
                           "app": {"slug": "github-actions"}, "details_url": row["link"]}
                          for index, row in enumerate(self.check_rows())]
                if "--jq" in args:
                    return json.dumps(checks)
                return json.dumps({"total_count": len(checks), "check_runs": checks})
        return super().command(args, input)


@pytest.fixture
def queued_case(git_history, tmp_path, monkeypatch):
    def build(*, queued, running):
        case = mode_case(git_history, tmp_path, monkeypatch)
        # The project's own run budget (.foragent.yml), so only the CI wait can end these runs early.
        object.__setattr__(case.config, "max_runtime_minutes", 360)
        case.ci = QueuedCI(case, monkeypatch, queued=queued, running=running)
        sleep = case.ctx.sleep

        def bounded_sleep(seconds):
            if case.ci.first_read and (case.clock[0] - case.ci.first_read).total_seconds() > RUNAWAY:
                case.ci.runaway = True
                raise AssertionError("The CI wait never ended")
            sleep(seconds)

        case.ctx.sleep = bounded_sleep
        return case

    return build


def held_after(case):
    """Seconds from the first CI read to the hold."""
    assert case.ci.first_read is not None, "The gate never read CI"
    return (case.clock[0] - case.ci.first_read).total_seconds()


def assert_ci_hold(case, outcome):
    assert not case.ci.runaway, "The CI wait must end on its own"
    assert outcome == "timeout", f"CI that never finishes must still be held as a timeout, got {outcome!r}"
    flag = case.store.load_flag(1)
    assert flag is not None and flag.reason == Reason.TIMEOUT
    assert case.tracker.merged_prs == [], "Unfinished CI must not merge"
    assert case.tracker.get_issue(2).state == "open", "The hold keeps the PR open"
    assert len(case.implementer.calls) == 1, "Unfinished CI is not a code failure: no worker turn"


def test_green_after_a_long_github_queue_merges_without_a_hold(queued_case):
    case = queued_case(queued=QUEUED, running=RUNNING)

    outcome = run(case)

    assert not case.ci.runaway
    assert case.store.load_flag(1) is None, (
        f"{QUEUED // 60} min queued on GitHub plus {RUNNING // 60} min running must not be held "
        f"(flag: {case.store.load_flag(1)})"
    )
    assert outcome == "merged" and case.tracker.merged_prs == [2], (
        f"All-green CI after a long queue must merge, got {outcome!r}"
    )
    assert held_after(case) >= QUEUED + RUNNING, "It merged before CI finished"
    assert len(case.implementer.calls) == 1, "Waiting for CI must not cost a worker turn"


@pytest.mark.parametrize("queued", [0, QUEUED], ids=["started-at-once", "after-a-long-queue"])
def test_running_past_the_limit_is_still_held(queued_case, queued):
    case = queued_case(queued=queued, running=None)

    outcome = run(case)

    assert_ci_hold(case, outcome)
    running_for = held_after(case) - queued
    assert LIMIT - 60 <= running_for <= LIMIT + 60, (
        f"A run in progress is held once it has run for the CI wait's limit ({LIMIT} s), "
        f"counted from when it started; it was held after {running_for:.0f} s of running"
    )


def test_a_run_that_never_leaves_the_queue_is_held_after_a_longer_ceiling(queued_case):
    case = queued_case(queued=None, running=None)

    outcome = run(case)

    assert_ci_hold(case, outcome)
    waited = held_after(case)
    assert waited >= 2 * LIMIT, (
        f"A queued run is held only after a ceiling much longer than the CI wait; held after {waited:.0f} s"
    )
