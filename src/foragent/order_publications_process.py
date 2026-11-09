"""One gate-started publication red-check process, independent of the merge owner.

The process-lifetime intent prevents overlapping gate ticks from duplicating checks.
The process exits after one pass; a later tick retries work left pending by a crash.
"""

from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys

from foragent import capacity, orch_github, orch_stop, order_publications, runner
from foragent.file_locks import reporting
from foragent import state_claims
from foragent.tracker import TrackerError


LOCK = "order-publication-checks"


def start(ctx, report):
    """Record a separate launch intent; start the child after releasing its file lock."""
    if (not getattr(ctx.config, "merge_owner", False)
            or not order_publications.pending(ctx.store)
            or orch_stop.active(ctx.store) is not None
            or ctx.store.github_wait_until(ctx.now())):
        return
    intent = ctx.store.root / f"{LOCK}.intent"
    token = state_claims.acquire(intent)
    if token is None:
        return
    try:
        request = {"repo": ctx.repo, "repo_root": str(ctx.repo_root.resolve()),
                   "store_root": str(ctx.store.root.resolve()), "config": asdict(ctx.config),
                   "committed_config": getattr(ctx, "committed_config", False)}
        with (ctx.store.root / f"{LOCK}.log").open("ab") as stream:
            child = subprocess.Popen(
                [sys.executable, "-m", "foragent.order_publications_process", json.dumps(request), token],
                stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                start_new_session=True,
                env={**{name: value for name, value in os.environ.items() if name != capacity.TEST_SLOT_ENV},
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
        state_claims.handoff(intent, token, child.pid)
        report.actions.append("order publication checks started")
    except Exception as exc:
        state_claims.release(intent, token, pid=os.getpid())
        report.actions.append(f"order publication checks start deferred: {exc}")


def run_process(request, token):
    from foragent.config import Config
    from foragent.git import GitRepo
    from foragent.scheduler import TickReport
    from foragent.scheduler_config import fetch_base
    from foragent.state import StateStore
    from foragent.tracker import GhTracker

    store_root = Path(request["store_root"])
    with state_claims.process_owner(store_root / f"{LOCK}.intent", token) as owned, reporting(store_root):
        if not owned:
            return
        root = Path(request["repo_root"])
        ctx = runner.RunContext(request["repo"], root, GhTracker(request["repo"]),
                                StateStore(store_root), Config(**request["config"]),
                                GitRepo(root), lambda: datetime.now(UTC))
        ctx.committed_config = request.get("committed_config", False)
        report = TickReport()
        if (ctx.config.merge_owner and orch_stop.active(ctx.store) is None
                and not ctx.store.github_wait_until(ctx.now())):
            try:
                fetch_base(ctx)
                if ctx.config.merge_owner:
                    order_publications.check_pending(ctx, report)
            except TrackerError as exc:
                if not exc.waits:
                    raise
                orch_github.pause(ctx, exc)
        for action in report.actions:
            print(action, flush=True)


if __name__ == "__main__":
    run_process(json.loads(sys.argv[1]), sys.argv[2])
