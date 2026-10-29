"""Throwaway live doctor tasks, kept separate from the issue runner."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import yaml

from foragent.config import Config
from foragent.executors import ExecResult, Executor, executor_for
from foragent.git import GitRepo
from foragent.issue import body_hash
from foragent.labels import APPROVED, RUNNING
from foragent.run_attempt import ExecutorStopped
from foragent.run_checks import _check
from foragent.state import StateStore
from foragent.tracker import FakeTracker

def lane_skill_drift(repo_root: Path, config: Config) -> list[str]:
    """Lanes whose agent frontmatter skills differ from .foragent.yml, their single source (D95).

    Only reports; ``fa lanes skills`` is the writer that repairs a projection.
    """
    problems = []
    for lane, settings in config.lanes.items():
        agent = settings.get("agent") or f"fa-{lane}"
        path = repo_root / ".claude/agents" / f"{agent}.md"
        if not isinstance(agent, str) or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.match(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|$)", text, re.S | re.M)
        try:
            metadata = yaml.safe_load(match[1]) if match else None
        except yaml.YAMLError:
            metadata = None
        declared = metadata.get("skills", []) if isinstance(metadata, dict) else None
        configured = settings.get("skills", [])
        if declared != configured:
            problems.append(f"FAIL lane {lane}: skills differ: .foragent.yml={configured!r}, "
                            f".claude/agents/{agent}.md={declared!r}; run `fa lanes skills {lane} ...` to rewrite both")
    return problems


SMOKE_FILE = "hello.txt"
SMOKE_CHECK = f'test "$(cat {SMOKE_FILE})" = ok'


@dataclass(frozen=True)
class LiveCheck:
    executor: str
    passed: bool
    detail: str

    def line(self) -> str:
        return f"{'PASS' if self.passed else 'FAIL'} {self.executor}: {self.detail}"


def _one_line(text: str) -> str:
    return " / ".join(line.strip() for line in text.strip().splitlines() if line.strip())


DOCTOR_ISSUE = 1
DOCTOR_BODY = f"""## Goal
Doctor smoke task: create the file {SMOKE_FILE} at the repository root containing exactly the line `ok`.

## Out of scope
Changing any other file.

## Done when
```sh
{SMOKE_CHECK}
```
"""


class _RecordedExecutor:
    """Keep the real adapter's last result so the doctor can quote the executor itself."""

    def __init__(self, executor: Executor):
        self.executor = executor
        self.result: ExecResult | None = None

    def run(self, **kwargs) -> ExecResult:
        self.result = self.executor.run(**kwargs)
        return self.result


def live_check(repo_root: Path, config: Config, executor_name: str, *, timeout_s: int = 900) -> LiveCheck:
    """Run one executor through the issue runner's implementation step on a throwaway clone.

    The approval snapshot, run record and tracker are local and temporary, so the
    runner's own implementer setup (lane-adjusted model and tier, Claude settings
    with the check-done Stop hook, /goal, availability handling) runs unchanged
    without touching GitHub, the user's checkout or the project's state.
    """
    from foragent.runner import RunContext
    from foragent.run_implementation import _implement, _prepare_implementation

    if shutil.which(executor_name) is None:
        return LiveCheck(executor_name, False, f"`{executor_name}` executable not found on PATH")
    config = replace(
        config, executor=executor_name, board=None,
        # A doctor reports unavailability instead of waiting it out.
        transient_max_wait_minutes=0,
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="foragent-doctor-") as temporary:
        clone = Path(temporary) / "repo"
        # A clone, unlike a worktree, leaves the user's repository metadata untouched.
        cloned = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(repo_root), str(clone)],
            capture_output=True, text=True,
        )
        if cloned.returncode != 0:
            return LiveCheck(executor_name, False, "temporary clone failed: " + _one_line(cloned.stderr))
        # Without a remote, nothing the executor does can reach the original repository.
        subprocess.run(["git", "-C", str(clone), "remote", "remove", "origin"], capture_output=True)
        # A committed artifact must not let an executor that did nothing pass the check.
        (clone / SMOKE_FILE).unlink(missing_ok=True)
        store = StateStore(Path(temporary) / "state")
        now = lambda: datetime.now(UTC)  # noqa: E731
        store.save_approval(
            DOCTOR_ISSUE, body=DOCTOR_BODY, body_sha=body_hash(DOCTOR_BODY), acceptance_blobs={},
            approved_by="fa-doctor", approved_at=now().isoformat(),
        )
        tracker = FakeTracker(now=now)
        tracker.add_issue(DOCTOR_ISSUE, "fa doctor --live smoke task", DOCTOR_BODY, [APPROVED, RUNNING])
        store.start_run(DOCTOR_ISSUE, branch="foragent/doctor", worktree=clone)
        recorded = _RecordedExecutor(executor_for(executor_name))
        ctx = RunContext(
            repo=config.repo or "local/doctor", repo_root=clone, tracker=tracker, store=store,
            config=config, git=GitRepo(clone), now=now, executor_factory=lambda name: recorded,
            deadline_seconds=max(60, timeout_s),
        )
        stopped = ""
        try:
            attempt = _prepare_implementation(
                DOCTOR_ISSUE, ctx, ctx.git, target_branch=config.base_branch, capture_baseline=False,
            )
            _implement(attempt, "")
        except ExecutorStopped as exc:
            stopped = f"runner stopped ({exc.outcome})"
        except OSError as exc:
            return LiveCheck(executor_name, False,
                             f"`{executor_name}` could not start (not found or not executable): {exc}")
        seconds = time.monotonic() - started
        result = recorded.result
        log = store.log_path(DOCTOR_ISSUE)
        tail = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.is_file() else ""
        said = _one_line((result.last_message or result.log_tail if result else "") or tail)
        turns = "unknown turns" if result is None or result.num_turns is None else (
            f"{result.num_turns} turn" + ("" if result.num_turns == 1 else "s"))
        stats = f"{turns}, {seconds:.1f} seconds"
        if result is None:
            return LiveCheck(executor_name, False, f"{stopped or 'executor did not run'} ({stats}): {said}")
        if result.timed_out:
            return LiveCheck(executor_name, False, f"timed out after {stats}; executor said: {said}")
        if result.exit_code != 0:
            return LiveCheck(executor_name, False,
                             f"exit code {result.exit_code} ({stats}); executor said: {said}")
        if result.num_turns == 0 or stopped:
            return LiveCheck(executor_name, False,
                             f"{stopped or 'stopped before any work'} ({stats}); executor said: {said}")
        for command in attempt.parsed.done_when_commands:
            check = _check(command, clone, log, 60)
            if check.exit_code != 0 or check.timed_out:
                return LiveCheck(executor_name, False,
                                 f"Done when check failed: {command} ({stats}); executor said: {said}")
        return LiveCheck(executor_name, True, f"smoke task done in {stats}; executor said: {said}")
