"""One silence decision shared by checks, baseline captures and run runtimes (#435).

``decide(facts)`` is the only decider and ``outcome_for(kind)`` the only mapper from its
kind to a run outcome; no other module branches on watchdog kinds. Work is silent only
when its output, its progress and the CPU time of its recorded process tree all stopped
growing for the whole window: twice the longest gap between results this repository
recorded, or the default (or configured) window. Without an earlier sample the reference
point is the start, and growth is 0. Unknown CPU (an unreadable table or process) is
never silence: then only the safety net stops. The safety net ends work even while it
progresses. Nothing here enumerates or signals processes: callers supply the snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from math import isfinite
from pathlib import Path

DEFAULT_WINDOW_SECONDS = 600
SAFETY_NET_SECONDS = 24 * 60 * 60
# The run outcome of each stopping kind; precedence in one observation follows decide().
OUTCOMES = {"safety-net": "stalled-restart", "runtime-limit": "killed", "silent": "stalled-restart"}


@dataclass(frozen=True)
class Facts:
    """What a decision reads, all from one observation."""

    silence_age: float  # Since the latest output, progress or tree CPU growth (or the start).
    window_seconds: float
    elapsed: float
    safety_net: float
    cpu_known: bool
    runtime_limit: float | None = None  # A run's max_runtime_minutes (with its grace), if any.


def decide(facts: Facts) -> str:
    """running, silent, runtime-limit or safety-net; safety-net > runtime-limit > silent."""
    if facts.elapsed >= facts.safety_net:
        return "safety-net"
    silent = facts.cpu_known and facts.silence_age >= facts.window_seconds
    if silent and facts.runtime_limit is not None and facts.elapsed > facts.runtime_limit:
        return "runtime-limit"
    return "silent" if silent else "running"


def stopped_check_kind(stop: str | None, run_elapsed: float, config) -> str:
    """The run's kind for a check the watchdog stopped, decided with the run's own facts.

    The check was silent (or reached its own safety net); the run's elapsed time is then
    measured against the run's safety net and runtime limit, so decide()'s precedence holds:
    a run past its safety net restarts, even when the check itself stopped as silent.
    """
    from foragent.config import safety_net_seconds

    return decide(Facts(silence_age=1, window_seconds=1,
                        elapsed=float("inf") if stop == "safety-net" else run_elapsed,
                        safety_net=safety_net_seconds(config), cpu_known=True,
                        runtime_limit=runtime_limit_seconds(config)))


def silence_window(config) -> tuple[float, str]:
    """A run's silence window and its source: "config" when silence_minutes is set, else "default"."""
    if config.silence_minutes is None:
        return DEFAULT_WINDOW_SECONDS, "default"
    return config.silence_minutes * 60, "config"


def runtime_limit_seconds(config) -> float:
    """max_runtime_minutes with main's ten-minute grace: past it, a silent run is a runtime-limit."""
    return (config.max_runtime_minutes + 10) * 60


def outcome_for(kind: str) -> str | None:
    """The run outcome for a stopping kind: stalled-restart keeps the WIP and returns to ready."""
    return OUTCOMES.get(kind)


@dataclass(frozen=True)
class Decision:
    stop: bool
    decision: str  # The logged fact: "running", "silent" (also past the runtime limit) or "safety-net".
    last_output_age: float
    cpu_delta: float
    window_seconds: float
    window_source: str  # "history", "config" or "default"
    grew: bool  # Output, progress or tree CPU grew since the previous observation.
    cpu_known: bool
    kind: str  # decide()'s kind
    outcome: str | None  # outcome_for(kind)

    def facts(self) -> dict:
        return asdict(self)


def _number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value) and value > 0


class SilenceWatchdog:
    """The samples of one piece of work; each observation is decided by decide()."""

    def __init__(self, history_gaps=(), safety_net: float = SAFETY_NET_SECONDS,
                 window_seconds: float | None = None, started: float | None = None,
                 runtime_limit: float | None = None):
        gaps = [gap for gap in history_gaps if _number(gap)]
        if gaps:
            self.window_seconds, self.window_source = 2 * max(gaps), "history"
        elif window_seconds is not None:  # A configured window, even if it equals the default.
            self.window_seconds, self.window_source = window_seconds, "config"
        else:
            self.window_seconds, self.window_source = DEFAULT_WINDOW_SECONDS, "default"
        self.safety_net, self.runtime_limit = safety_net, runtime_limit
        self.started = started
        self.last_output_at: float | None = started
        self.last_activity_at: float | None = started
        self.output_bytes: int | None = None
        self.progress_events: int | None = None
        self.cpu: dict[str, float] = {}
        self.result_times: list[float] = []
        self.last: Decision | None = None

    @property
    def result_gaps(self) -> tuple[float, ...]:
        """Gaps between consecutive results (output growth), from the start."""
        times = [self.started, *self.result_times] if self.started is not None else self.result_times
        return tuple(round(later - earlier, 3) for earlier, later in zip(times, times[1:]))

    def observe(self, now: float, output_bytes: int, progress_events: int, snapshot, identities) -> Decision:
        recorded = {(item["pid"], item["start_time"]) for item in identities}
        cpu_known = snapshot is not None  # None: the process table was unreadable.
        if {pid for pid, _ in recorded} & set(getattr(snapshot, "unreadable", ()) or ()):
            cpu_known = False  # A recorded process the table could not read is unknown, not absent.
        cpu = {}
        for row in () if snapshot is None else snapshot.rows.values() if hasattr(snapshot, "rows") else snapshot:
            if (row.pid, row.start_time) not in recorded:
                continue  # Only the recorded lifetime counts: a reused PID is another process.
            # A row type without CPU carries no growth; a CPU read that failed (None) is unknown.
            seconds = getattr(row, "cpu_time", 0.0)
            if seconds is None:
                cpu_known = False
            else:
                cpu[f"{row.pid}:{row.start_time}"] = float(seconds)
        if self.started is None:
            self.started = self.last_output_at = self.last_activity_at = now
        first = self.output_bytes is None  # No earlier sample: no growth, the start is the reference.
        cpu_delta = 0.0 if first else sum(max(0.0, seconds - self.cpu.get(key, 0.0)) for key, seconds in cpu.items())
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
        kind = decide(Facts(now - self.last_activity_at, self.window_seconds, now - self.started,
                            self.safety_net, cpu_known, self.runtime_limit))
        logged = "running" if kind == "running" else "safety-net" if kind == "safety-net" else "silent"
        self.last = Decision(kind != "running", logged, now - self.last_output_at, cpu_delta, self.window_seconds,
                             self.window_source, grew, cpu_known, kind, outcome_for(kind))
        return self.last

    def state(self) -> dict:
        return {"last_output_at": self.last_output_at, "last_activity_at": self.last_activity_at,
                "output_bytes": self.output_bytes, "progress_events": self.progress_events, "cpu": self.cpu}

    def restore(self, state: dict) -> None:
        for name in ("last_output_at", "last_activity_at", "output_bytes", "progress_events"):
            if state.get(name) is not None:
                setattr(self, name, state[name])
        self.cpu = dict(state.get("cpu") or {})


def recorded_gaps(cache_root: Path, repository: Path) -> tuple[float, ...]:
    """Result gaps of this repository's latest capture that recorded them (main's capture history)."""
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


def silence_args(ctx) -> dict:
    """The watchdog inputs a check site passes to ``run_checks._check``."""
    from foragent.config import safety_net_seconds

    return {"history_gaps": recorded_gaps(ctx.store.root.parent / "baselines", ctx.repo_root),
            "safety_net": safety_net_seconds(ctx.config)}
