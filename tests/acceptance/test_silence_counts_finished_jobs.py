"""D89, train #2585 (11-07): a finished tip CI job is merge batch progress.

World: the real runner/scheduler/Git/state merge-batch world of
test_batch_ends_on_silence (offline FakeTracker, fake clock). Each owner tick
uses a fresh StateStore, so progress must come from the durable record.

Contract for tip CI jobs in the offline tracker: they are published with the
existing ``FakeTracker.set_workflow_runs`` in GitHub REST shape (a workflow run
with ``head_sha``/``head_branch``/``status`` and its REST jobs under ``jobs``,
each job with ``status``/``conclusion``/``started_at``/``completed_at``). A job
counts when ``status == "completed"``; the owner records the time it first sees
it, not the job's ``completed_at``. Runs of other heads never count.

The owner's merge-owner.log is its stdout/stderr; here the owner runs in
process, so its lines are read from the captured output.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
import sys

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/foragent").is_dir())
for directory in (ROOT / "tests", ROOT / "tests/acceptance"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from acceptance.test_merge_path_real_settings import world  # noqa: E402, F401
from test_merge_owner_proto import proto  # noqa: E402, F401
import test_merge_owner_proto as support  # noqa: E402
from test_merge_owner_batch import batched, events, green, groups, refs  # noqa: E402, F401
from foragent import merge_owner_batch as batch  # noqa: E402

SILENCE = timedelta(seconds=batch.SILENCE_SECONDS)
MINUTE = timedelta(minutes=1)
SILENCE_REASON = "after 60 minutes of silence"


def iso(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(when):
    return when.astimezone(UTC).strftime("%H:%M")


class TipJobs:
    """GitHub Actions jobs of one head, as REST runs with their jobs."""

    def __init__(self, case, head, branch, run_id=9100):
        self.case, self.head, self.branch, self.run_id = case, head, branch, run_id
        self.jobs = []
        self.others = []  # Runs of other heads: never tip progress.

    def _publish(self):
        unfinished = any(job["status"] != "completed" for job in self.jobs)
        run = {
            "id": self.run_id, "name": "foragent-acceptance", "head_sha": self.head,
            "head_branch": self.branch, "event": "pull_request",
            "status": "in_progress" if unfinished or not self.jobs else "completed",
            "conclusion": None if unfinished or not self.jobs else "success",
            "jobs": [dict(job) for job in self.jobs],
        }
        self.case.tracker.set_workflow_runs([run, *self.others])

    def add(self, name, *, status="queued", started=None, completed=None, conclusion=None):
        self.jobs.append({
            "id": self.run_id * 100 + len(self.jobs) + 1, "run_id": self.run_id, "name": name,
            "head_sha": self.head, "head_branch": self.branch, "status": status,
            "conclusion": conclusion, "started_at": started and iso(started),
            "completed_at": completed and iso(completed),
        })
        self._publish()

    def start(self, name, when):
        job = next(job for job in self.jobs if job["name"] == name)
        job.update(status="in_progress", started_at=iso(when))
        self._publish()

    def finish(self, name, when, conclusion="success"):
        job = next(job for job in self.jobs if job["name"] == name)
        job.update(status="completed", conclusion=conclusion, completed_at=iso(when),
                   started_at=job["started_at"] or iso(when - MINUTE))
        self._publish()

    def other_head_finished(self, head, branch, name, when):
        run_id = self.run_id + 1 + len(self.others)
        self.others.append({
            "id": run_id, "name": "foragent-acceptance", "head_sha": head, "head_branch": branch,
            "event": "pull_request", "status": "completed", "conclusion": "success",
            "jobs": [{"id": run_id * 100 + 1, "run_id": run_id, "name": name, "head_sha": head,
                      "head_branch": branch, "status": "completed", "conclusion": "success",
                      "started_at": iso(when - MINUTE), "completed_at": iso(when)}],
        })
        self._publish()


def pending_batch(case, members=(5, 1)):
    """Publish a merge batch whose tip CI stays pending, with its jobs queued."""
    case.pending_batch = True
    support.queue_workers(case, list(members))
    support.gate(case)
    pr, = groups(case)
    tip = case.tracker.pr_head_sha(pr.number)
    assert refs(case) and not events(case) and not batch.retirements(case.ctx)
    case.tracker.set_pr_checks(pr.number, "pending")
    jobs = TipJobs(case, tip, pr.head)
    for name in ("acceptance (shard 0)", "acceptance (shard 1)", "acceptance (shard 2)",
                 "acceptance (shard 3)", "unit (shard 0)", "unit (shard 1)"):
        jobs.add(name)
    # The batch is built (last progress) when its first merge commit was made.
    born = datetime.fromtimestamp(
        int(case.git._call(["show", "-s", "--format=%ct", tip]).strip()), tz=UTC)
    return case.history.tip, tip, pr, jobs, born


def tick_at(case, when):
    """One owner tick at ``when`` (gate itself adds one second)."""
    case.clock[0] = when - timedelta(seconds=1)
    support.gate(case)


def assert_waiting(case, message):
    assert refs(case), f"{message}: the batch's published reservation is gone"
    assert not batch.retirements(case.ctx), f"{message}: retired {batch.retirements(case.ctx)}"
    assert not events(case) and not case.target_pushes and not case.merges


def assert_retired_for_silence(case, base, tip):
    assert not case.target_attempts and not case.target_pushes and not case.merges
    assert case.history.tip == base and not refs(case)
    retirement, = batch.retirements(case.ctx)
    assert retirement["candidate"] == tip
    assert SILENCE_REASON in retirement["reason"], retirement["reason"]
    assert not retirement["spent"], "Silence must not spend the members' publications"
    event, = events(case)
    assert event["result"] == "unused" and event["used_members"] == 0
    assert all(case.tracker.get_issue(n).state == "open" for n in (5, 1))
    return retirement


def silence_lines(output):
    return [line for line in output.splitlines() if "retired" in line and "silence" in line]


# --- Statement 1: finished tip jobs keep a pending batch alive ------------------------------------


def test_tip_jobs_finishing_within_the_window_keep_the_batch_until_ci_passes(batched, capsys):
    """Train #2585: jobs finish one by one under runner queueing; the batch must not retire."""
    case = batched
    base, tip, pr, jobs, born = pending_batch(case)
    start = case.clock[0]
    names = [job["name"] for job in jobs.jobs]
    # Each job finishes 50 minutes after the previous progress; the owner polls in between.
    for k, name in enumerate(names[:4], start=1):
        seen = start + k * 50 * MINUTE
        tick_at(case, seen - 25 * MINUTE)  # A poll with nothing new.
        assert_waiting(case, f"poll before job {k}")
        jobs.finish(name, seen - 3 * MINUTE)
        tick_at(case, seen)
        assert_waiting(case, f"after job {k} ({name}) finished")
    assert case.clock[0] - start >= 3 * SILENCE, "The batch must live well past one silence window"
    # 55 minutes after the last finished job: still inside the window.
    tick_at(case, case.clock[0] + 55 * MINUTE)
    assert_waiting(case, "55 minutes after the last finished job")
    assert "silence" not in capsys.readouterr().out

    # The rest of tip CI passes: the batch lands once, as the hand-merge of r1080 would have.
    for name in names[4:]:
        jobs.finish(name, case.clock[0])
    green(case)
    support.gate(case)
    assert case.target_pushes == [(tip, "refs/heads/main", base)], (
        "Finished tip CI jobs must count as progress; "
        f"retired instead: {batch.retirements(case.ctx)}"
    )
    assert case.history.tip == tip and not case.merges and not refs(case)
    assert {n for n, _ in case.store.owner_merge_facts()} == {5, 1}
    event, = events(case)
    assert event["result"] == "used" and event["used_members"] == 2
    support.gate(case)
    assert case.target_pushes == [(tip, "refs/heads/main", base)], "A replay must not land twice"


def test_a_finished_job_counts_once_from_when_the_owner_first_sees_it(batched, capsys):
    """Progress time is the owner's first sight; re-reading the same finished job is not progress."""
    case = batched
    base, tip, pr, jobs, born = pending_batch(case)
    start = case.clock[0]
    # The runner finished the job at +10, but the owner first sees it at +30.
    jobs.finish("unit (shard 1)", start + 10 * MINUTE)
    seen = start + 30 * MINUTE
    tick_at(case, seen)
    assert_waiting(case, "when the finished job is first seen")
    # Later polls list the same finished job again (fresh owner store each tick: a restart).
    for offset in (55, 65, 75, 85, 89):
        tick_at(case, start + offset * MINUTE)
        assert_waiting(case, f"{offset} minutes after build, {offset - 30} after the job was seen")
    assert not silence_lines(capsys.readouterr().out)

    tick_at(case, seen + SILENCE + MINUTE)
    retirement = assert_retired_for_silence(case, base, tip)
    assert pr.number not in case.tracker.rerun_cancelled_prs
    lines = silence_lines(capsys.readouterr().out)
    assert lines, "merge-owner.log must have the silence retirement line"
    assert any("unit (shard 1)" in line and hhmm(seen) in line for line in lines), (
        "The retirement line must name the last progress seen (the finished job) and when "
        f"the owner saw it ({hhmm(seen)} UTC): {lines}; reason {retirement['reason']!r}"
    )


# --- Statement 2: no finished job and no other progress still retires --------------------------


def test_tip_without_finished_jobs_retires_for_silence_naming_its_last_progress(batched, capsys):
    """Jobs that only queue or start, and other heads' jobs, are not tip progress."""
    case = batched
    base, tip, pr, jobs, born = pending_batch(case)
    start = case.clock[0]
    member = support.pr_for(case, 5)
    tick_at(case, start + 15 * MINUTE)
    jobs.start("acceptance (shard 0)", start + 15 * MINUTE)  # Started, not finished.
    jobs.other_head_finished(case.tracker.pr_head_sha(member.number), member.head,
                             "unit (shard 0)", start + 20 * MINUTE)
    for offset in (25, 40, 55):
        tick_at(case, start + offset * MINUTE)
        assert_waiting(case, f"{offset} minutes into silence")
    assert not silence_lines(capsys.readouterr().out)

    tick_at(case, born + SILENCE + MINUTE)
    assert_retired_for_silence(case, base, tip)
    lines = silence_lines(capsys.readouterr().out)
    assert len(lines) == 1, f"One owner retirement line for one silence retirement: {lines}"
    line, = lines
    assert hhmm(born) in line and re.search(r"\b(buil[td]|publish|born|creat)", line, re.I), (
        f"The retirement line must name the last progress (the batch build at {hhmm(born)} UTC): {line!r}"
    )
    assert "unit (shard 0)" not in line, "Another head's job is not this batch's progress"
    support.gate(case)
    assert len(batch.retirements(case.ctx)) == 1 and len(events(case)) == 1


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_silence_after_the_last_finished_job_retires_and_names_that_job(batched, capsys, conclusion):
    """A job that finished earlier does not hold the batch forever; a failed job is still a final state."""
    case = batched
    base, tip, pr, jobs, born = pending_batch(case)
    start = case.clock[0]
    jobs.finish("acceptance (shard 2)", start + 39 * MINUTE, conclusion)
    seen = start + 40 * MINUTE
    tick_at(case, seen)
    tick_at(case, born + SILENCE + 5 * MINUTE)
    assert_waiting(case, "past the build's window but inside the finished job's window")
    tick_at(case, seen + SILENCE - MINUTE)
    assert_waiting(case, "one minute before the finished job's window ends")
    capsys.readouterr()

    tick_at(case, seen + SILENCE + MINUTE)
    assert_retired_for_silence(case, base, tip)
    lines = silence_lines(capsys.readouterr().out)
    assert len(lines) == 1, f"One owner retirement line for one silence retirement: {lines}"
    assert "acceptance (shard 2)" in lines[0] and hhmm(seen) in lines[0], (
        f"The retirement line must name the last finished job and when it was seen ({hhmm(seen)} UTC): "
        f"{lines[0]!r}"
    )
