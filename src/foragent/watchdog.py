"""One silence decision shared by checks, baseline captures and run runtimes (#435).

Work is silent only when its output, its progress events and the CPU time of
its recorded process tree all stopped growing for the whole window. The window
is twice the longest gap between results this repository recorded, or the
default (or configured) window without such history. The safety net ends work
after a generous elapsed time even while it still progresses. The decision
itself never enumerates or signals processes: callers supply the snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from math import isfinite
from pathlib import Path

DEFAULT_WINDOW_SECONDS = 600
SAFETY_NET_SECONDS = 24 * 60 * 60
# Past its runtime limit, a session that shows no growth for one gate tick is not progressing.
RUNTIME_QUIET_SECONDS = 60


@dataclass(frozen=True)
class Decision:
    stop: bool
    decision: str  # "running", "silent" or "safety-net"
    last_output_age: float
    cpu_delta: float
    window_seconds: float
    window_source: str  # "history", "config" or "default"
    grew: bool  # Any of output, progress or tree CPU grew since the previous observation.
    # False when the process table or a recorded process's CPU could not be read:
    # unknown CPU is never silence, so only the safety net can stop such work.
    cpu_known: bool = True

    def facts(self) -> dict:
        return asdict(self)


def _number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value) and value > 0


class SilenceWatchdog:
    """``runtime_limit`` (an executor session's ``max_runtime_minutes`` budget) ends work
    past it only once it is also quiet for ``RUNTIME_QUIET_SECONDS``, never while it grows."""

    def __init__(self, history_gaps=(), safety_net: float = SAFETY_NET_SECONDS,
                 window_seconds: float | None = None, started: float | None = None,
                 runtime_limit: float | None = None):
        gaps = [gap for gap in history_gaps if _number(gap)]
        if gaps:
            self.window_seconds, self.window_source = 2 * max(gaps), "history"
        elif window_seconds is not None and window_seconds != DEFAULT_WINDOW_SECONDS:
            self.window_seconds, self.window_source = window_seconds, "config"
        else:
            self.window_seconds, self.window_source = DEFAULT_WINDOW_SECONDS, "default"
        self.safety_net = safety_net
        self.runtime_limit = runtime_limit
        self.started = started
        self.last_output_at: float | None = started
        self.last_activity_at: float | None = started
        self.output_bytes: int | None = None
        self.progress_events: int | None = None
        self.cpu: dict[str, float] = {}
        self.result_times: list[float] = []

    @property
    def result_gaps(self) -> tuple[float, ...]:
        """Gaps between consecutive results (output growth), from the start."""
        times = [self.started, *self.result_times] if self.started is not None else self.result_times
        return tuple(round(later - earlier, 3) for earlier, later in zip(times, times[1:]))

    def observe(self, now: float, output_bytes: int, progress_events: int, snapshot, identities) -> Decision:
        recorded = {(item["pid"], item["start_time"]) for item in identities}
        cpu_known = snapshot is not None  # None: the process table was unreadable.
        # A recorded process the table could not read is unknown too, not absent.
        if {pid for pid, _ in recorded} & set(getattr(snapshot, "unreadable", ()) or ()):
            cpu_known = False
        cpu = {}
        for row in () if snapshot is None else snapshot.rows.values() if hasattr(snapshot, "rows") else snapshot:
            # Only the recorded lifetime counts: a reused PID is another process.
            if (row.pid, row.start_time) not in recorded:
                continue
            seconds = getattr(row, "cpu_time", None)
            if seconds is None:
                cpu_known = False  # A live recorded process whose CPU could not be read.
            else:
                cpu[f"{row.pid}:{row.start_time}"] = float(seconds)
        if self.started is None:
            self.started = self.last_output_at = self.last_activity_at = now
        first = self.output_bytes is None
        cpu_delta = sum(max(0.0, seconds - self.cpu.get(key, seconds if first else 0.0))
                        for key, seconds in cpu.items())
        output_grew = not first and (output_bytes > self.output_bytes or progress_events > self.progress_events)
        if not first and output_bytes > self.output_bytes:
            self.result_times.append(now)
        if output_grew:
            self.last_output_at = now
        grew = output_grew or cpu_delta > 0
        if grew:
            self.last_activity_at = now
        self.output_bytes, self.progress_events = output_bytes, progress_events
        self.cpu = {**self.cpu, **cpu}
        if now - self.started >= self.safety_net:
            decision = "safety-net"
        elif cpu_known and now - self.last_activity_at >= self.window_seconds:
            decision = "silent"
        elif (self.runtime_limit is not None and now - self.started >= self.runtime_limit
              and cpu_known and now - self.last_activity_at >= RUNTIME_QUIET_SECONDS):
            decision = "runtime-limit"
        else:
            decision = "running"
        return Decision(decision != "running", decision, now - self.last_output_at, cpu_delta,
                        self.window_seconds, self.window_source, grew, cpu_known)

    def state(self) -> dict:
        return {"started": self.started, "last_output_at": self.last_output_at,
                "last_activity_at": self.last_activity_at, "output_bytes": self.output_bytes,
                "progress_events": self.progress_events, "cpu": self.cpu}

    def restore(self, state: dict) -> None:
        for name in ("started", "last_output_at", "last_activity_at", "output_bytes", "progress_events"):
            setattr(self, name, state.get(name))
        self.cpu = dict(state.get("cpu") or {})


def recorded_gaps(cache_root: Path, repository: Path) -> tuple[float, ...]:
    """Result gaps of this repository's latest capture that recorded them."""
    target, latest = str(repository.resolve()), None
    for path in cache_root.glob("*.json"):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("repository") != target:
                continue
            gaps = saved.get("result_gaps_seconds")
            created = datetime.fromisoformat(saved["created_at"])
            if not isinstance(gaps, list) or not any(_number(gap) for gap in gaps):
                continue
            if latest is None or created > latest[0]:
                latest = created, tuple(gap for gap in gaps if _number(gap))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return () if latest is None else latest[1]


def history_gaps(ctx) -> tuple[float, ...]:
    """This repository's recorded result gaps; every check site sizes its silence window from them."""
    return recorded_gaps(ctx.store.root.parent / "baselines", ctx.repo_root)


def safety_net(ctx) -> float:
    from foragent.config import safety_net_seconds

    return safety_net_seconds(ctx.config)


def silence_args(ctx) -> dict:
    """The watchdog inputs a check site passes to ``run_checks._check``."""
    return {"history_gaps": history_gaps(ctx), "safety_net": safety_net(ctx)}


def hook_args(snapshot: Path, repository: Path | None, net: int | None) -> dict:
    """The Stop hook's watchdog inputs: its run's history and safety net, passed on its command line.

    ``snapshot`` is the run's approval under ``<state>/approvals``; the baseline cache sits
    beside the repository stores, as for every other check site.
    """
    cache = snapshot.parent.parent.parent / "baselines"
    return {"history_gaps": recorded_gaps(cache, repository) if repository is not None else (),
            "safety_net": net or SAFETY_NET_SECONDS}

