"""Parallel related checks: split the selected test files into groups by recorded seconds.

A related check that selects two or more files runs one pytest per group at the same
time, each holding its own machine test slot (``related_check_groups``, default 2;
1 keeps one serial pytest). The groups' results merge into one CheckResult, so the
run records the same related-test facts as a serial check.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import statistics
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from foragent.run_checks import CheckResult
    from foragent.runner import RunContext


def group_count(ctx: "RunContext", files: list[str]) -> int:
    """How many pytest processes this selection runs in (1 = today's serial check)."""
    groups = getattr(ctx.config, "related_check_groups", 1)
    if type(groups) is not int or groups < 2 or len(files) < 2:
        return 1
    return min(groups, len(files))


def recorded_seconds(ctx: "RunContext", n: int) -> dict[str, float]:
    """Each file's newest recorded related-check seconds (finished runs, then this run)."""
    seconds: dict[str, float] = {}
    runs = []
    try:
        runs.extend(ctx.store.read_history())
    except Exception:  # noqa: BLE001 - a history read only shapes the split, never the check
        pass
    try:
        if (current := ctx.store.get_run(n)) is not None:
            runs.append(current)
    except Exception:  # noqa: BLE001
        pass
    for run in runs:
        for check in getattr(run, "related_tests", None) or []:
            values = check.get("file_seconds") if isinstance(check, dict) else None
            for path, value in (values or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    seconds[path] = float(value)
    return seconds


def split(files: list[str], seconds: dict[str, float], count: int) -> list[list[str]]:
    """Largest file first into the lightest group; a file with no record counts as the median."""
    known = [seconds[path] for path in files if path in seconds]
    middle = statistics.median(known) if known else 0.0
    weight = {path: seconds.get(path, middle) for path in files}
    groups: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for path in sorted(files, key=lambda path: (-weight[path], files.index(path))):
        # Equal loads (no records at all) still spread by file count.
        index = min(range(count), key=lambda i: (loads[i], len(groups[i]), i))
        groups[index].append(path)
        loads[index] += weight[path]
    order = {path: index for index, path in enumerate(files)}
    return [sorted(group, key=order.__getitem__) for group in groups if group]


@dataclass
class _Span:
    """The command's own start and end on the check clock (slot queue excluded)."""

    clock: Callable[[], float] | None
    started: float | None = None
    ended: float | None = None

    def __call__(self) -> float:
        value = self.clock()
        if self.started is None:
            self.started = value
        self.ended = value
        return value


def run_groups(
    n: int, ctx: "RunContext", worktree: Path, commands: list[str], limit: int, log: Path, full_text_dir: Path,
    slot: Callable[[], object],
) -> "CheckResult":
    """Run each command at once under its own test slot and merge them like one serial check.

    Slots are taken in order on this thread (so a queued wait is one phase at a time); each
    group starts as soon as its slot is held. The last group runs on this thread, which keeps
    subprocess termination on the main thread working for it.
    """
    from foragent import runner
    from foragent.run_checks import CheckResult

    results: list[CheckResult | None] = [None] * len(commands)
    errors: list[BaseException] = []
    spans = [_Span(ctx.monotonic) if ctx.monotonic is not None else None for _ in commands]

    def check(index: int) -> None:
        span = spans[index]
        results[index] = runner._check(commands[index], worktree, log, limit, slot=nullcontext(),
                                       clock=span, full_text_dir=full_text_dir)

    def background(index: int, held) -> None:
        try:
            check(index)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            errors.append(exc)
        finally:
            held.__exit__(None, None, None)

    threads: list[threading.Thread] = []
    try:
        for index in range(len(commands)):
            held = slot()
            held.__enter__()
            if index == len(commands) - 1:
                try:
                    check(index)
                finally:
                    held.__exit__(None, None, None)
            else:
                thread = threading.Thread(target=background, args=(index, held), daemon=True)
                thread.start()
                threads.append(thread)
    finally:
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    done = [result for result in results if result is not None]
    codes = [result.exit_code for result in done]
    failed = [code for code in codes if code != 0]
    seconds = None
    if all(span is not None and span.started is not None for span in spans):
        seconds = max(span.ended for span in spans) - min(span.started for span in spans)
    output = "\n".join(f"Group {index + 1} of {len(done)}:\n{result.output}" for index, result in enumerate(done))
    return CheckResult("\n".join(result.command for result in done), failed[0] if failed else 0,
                       any(result.timed_out for result in done), output, seconds)
