"""Bounded subprocesses and process group termination."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


_process_group_callback: ContextVar[Callable[[int | None], None] | None] = ContextVar(
    "process_group_callback", default=None,
)


@contextmanager
def track_process_group(callback: Callable[[int | None], None]) -> Iterator[None]:
    """Observe subprocess ownership within one executor call without changing its API."""
    token = _process_group_callback.set(callback)
    try:
        yield
    finally:
        _process_group_callback.reset(token)


def kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(10)
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.killpg(pid, 0)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    # SIGKILL delivery is asynchronous. Reap a direct child where possible and
    # confirm exit before tick removes files the runner could still be using.
    for _ in range(50):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise TimeoutError(f"Runner process group {pid} did not terminate")


def _child_environment(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        env.pop(key, None)
    gate_bins = {(Path(__file__).resolve().parents[2] / ".venv/bin").resolve()}
    if sys.prefix != sys.base_prefix:
        gate_bins.add((Path(sys.prefix) / "bin").resolve())
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            entry for entry in env["PATH"].split(os.pathsep)
            if not entry or (cwd / entry).resolve() not in gate_bins
        )
    return env


def run_logged(
    argv: list[str], *, cwd: Path, timeout_s: int, log_path: Path,
    prompt: str | None = None, capture_stdout: bool = False,
    stdout_path: Path | None = None, terminate_children: bool = False,
    stderr_path: Path | None = None,
) -> tuple[int | None, bool, str]:
    """Append output without buffering an unbounded session in memory."""
    on_process_group = _process_group_callback.get()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log, (
        stdout_path.open("w+b") if stdout_path is not None else tempfile.TemporaryFile()
    ) as output, (stderr_path.open("ab") if stderr_path is not None else nullcontext()) as errors:
        process = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            stdout=output if capture_stdout else log,
            stderr=errors if errors is not None else log if capture_stdout else subprocess.STDOUT,
            start_new_session=True, env=_child_environment(cwd),
        )
        # Executors have their own session, so relay tick termination to them.
        previous = None
        can_handle = threading.current_thread() is threading.main_thread()

        def terminated(signum, frame):
            raise SystemExit(128 + signum)

        if can_handle:
            previous = signal.signal(signal.SIGTERM, terminated)
        timed_out = False
        forwarded = 0
        stop_forwarding = threading.Event()

        def forward_stdout():
            nonlocal forwarded
            output.flush()
            while chunk := os.pread(output.fileno(), 65536, forwarded):
                log.write(chunk)
                log.flush()
                forwarded += len(chunk)

        def follow_stdout():
            while not stop_forwarding.wait(1):
                forward_stdout()

        follower = threading.Thread(target=follow_stdout, daemon=True) if capture_stdout else None
        if follower is not None:
            follower.start()
        try:
            if on_process_group is not None:
                on_process_group(process.pid)  # start_new_session makes PID == PGID.
            try:
                process.communicate(input=prompt.encode("utf-8") if prompt is not None else None, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        finally:
            try:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                if terminate_children:
                    kill_process_group(process.pid)
                if on_process_group is not None:
                    on_process_group(None)
            finally:
                if can_handle:
                    signal.signal(signal.SIGTERM, previous)
                if follower is not None:
                    stop_forwarding.set()
                    follower.join()
                    forward_stdout()
        if capture_stdout:
            size = output.seek(0, 2)
            output.seek(max(0, size - 16000))
            tail = output.read().decode("utf-8", errors="replace")[-4000:]
        else:
            tail = ""
    return process.returncode, timed_out, tail
