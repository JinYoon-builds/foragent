"""Prepare local try results and manage disposable web previews without GitHub writes."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from foragent.git import GitRepo
from foragent.inspect_data import InspectContext, Prepared, PreparedCommand, collect, load_prepared
from foragent.processes import _child_environment, kill_process_group, run_logged


class InspectRuntimeError(ValueError):
    """A local try operation cannot be completed."""


def _git(ctx: InspectContext) -> GitRepo:
    if ctx.git is not None:
        return ctx.git
    if ctx.repo_root is None:
        raise InspectRuntimeError("repo-root is required for try worktrees")
    return GitRepo(ctx.repo_root)


def run_command(command: str, worktree: Path, log_path: Path) -> PreparedCommand:
    code, timed_out, _ = run_logged(
        ["/bin/sh", "-c", command], cwd=worktree, timeout_s=60, log_path=log_path,
        terminate_children=True,
    )
    with log_path.open("rb") as stream:
        stream.seek(max(0, log_path.stat().st_size - 12000))
        output = stream.read().decode("utf-8", errors="replace")[-3000:]
    return PreparedCommand(command, output, code, timed_out)


def prepare(ctx: InspectContext) -> list[int]:
    items = [item for item in collect(ctx).items
             if item.group == "try" and item.try_card is not None and item.try_card.kind == "cli"]
    if not items:
        return []
    git = _git(ctx)
    git.fetch("origin", ctx.config.base_branch)
    base_sha = git.ref_sha(f"origin/{ctx.config.base_branch}")
    prepared = []
    for item in items:
        previous = load_prepared(ctx.store, item.number)
        if previous is not None and previous.base_sha == base_sha:
            continue
        wt_root = ctx.store.root / "inspect/wt"
        wt_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{item.number}-", dir=wt_root) as directory:
            worktree = Path(directory)
            try:
                git.add_detached_worktree(worktree, base_sha)
                results = []
                for command in item.try_card.commands:
                    with tempfile.NamedTemporaryFile(dir=wt_root, suffix=".log") as log:
                        results.append(run_command(command, worktree, Path(log.name)))
                saved = ctx.store.root / "inspect/prepared" / f"{item.number}.json"
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_text(json.dumps(asdict(Prepared(base_sha, results)), ensure_ascii=False), encoding="utf-8")
                prepared.append(item.number)
            finally:
                git.remove_worktree(worktree)
    return prepared


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout_s: int) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.2)
    return False


def _served(ctx: InspectContext) -> dict:
    path = ctx.store.root / "inspect/served.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_served(ctx: InspectContext, records: dict) -> None:
    path = ctx.store.root / "inspect/served.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records), encoding="utf-8")


@contextmanager
def _served_lock(ctx: InspectContext) -> Iterator[None]:
    path = ctx.store.root / "inspect/served.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _stop_one(ctx: InspectContext, number: str, records: dict, killer: Callable[[int], None]) -> None:
    killer(records[number]["pid"])
    _git(ctx).remove_worktree(ctx.store.root / "inspect" / f"serve-{int(number)}")
    del records[number]
    _save_served(ctx, records)


def stop(ctx: InspectContext, *, killer: Callable[[int], None] | None = None) -> None:
    with _served_lock(ctx):
        records = _served(ctx)
        for number in list(records):
            _stop_one(ctx, number, records, killer or kill_process_group)


def tailscale_name() -> str | None:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(result.stdout).get("Self", {}).get("DNSName", "").rstrip(".") or None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def serve(
    ctx: InspectContext, number: int, *, process_launcher: Callable | None = None,
    port_waiter: Callable[[int, int], bool] | None = None,
    hostname: Callable[[], str | None] | None = None,
    killer: Callable[[int], None] | None = None,
) -> list[str]:
    item = next((item for item in collect(ctx).items if item.group == "try" and item.number == number), None)
    web = item.try_card.web if item and item.try_card and item.try_card.kind == "web" else None
    if web is None or not web.start.strip() or not 1 <= web.port <= 65535:
        raise InspectRuntimeError(f"#{number} needs a web try card with start and a valid port")
    git = _git(ctx)
    with _served_lock(ctx):
        records = _served(ctx)
        if str(number) in records:
            _stop_one(ctx, str(number), records, killer or kill_process_group)
        if _port_open(web.port):
            raise InspectRuntimeError(f"port {web.port} is already in use")
        git.fetch("origin", ctx.config.base_branch)
        base_sha = git.ref_sha(f"origin/{ctx.config.base_branch}")
        worktree = ctx.store.root / "inspect" / f"serve-{number}"
        git.remove_worktree(worktree)
        try:
            git.add_detached_worktree(worktree, base_sha)
            env = _child_environment(worktree)
            env["PORT"] = str(web.port)
            with (ctx.store.root / "inspect" / f"serve-{number}.log").open("ab") as log:
                process = (process_launcher or subprocess.Popen)(
                    ["/bin/sh", "-c", web.start], cwd=worktree, env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            records[str(number)] = {"pid": process.pid, "port": web.port}
            _save_served(ctx, records)
            if not (port_waiter or wait_for_port)(web.port, 90):
                raise InspectRuntimeError(f"#{number} did not open port {web.port} within 90 s")
        except BaseException:
            if str(number) in records:
                _stop_one(ctx, str(number), records, killer or kill_process_group)
            else:
                git.remove_worktree(worktree)
            raise
    path = web.path if web.path.startswith("/") else "/" + web.path
    urls = [f"http://localhost:{web.port}{path}"]
    if name := (hostname or tailscale_name)():
        urls.append(f"http://{name}:{web.port}{path}")
    return urls
