from dataclasses import asdict, replace
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import signal
import subprocess
from types import SimpleNamespace

import pytest

from foragent import inspect_actions, inspect_runtime as runtime
from foragent.config import Config
from foragent.git import GitRepo
from foragent.inspect_data import InspectContext, collect, counts_line, try_lines
from foragent.reasons import Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import FakeTracker
from foragent.trycard import parse_try

from conftest import BASE_TIME, git_command


CARD = """## What changed
보고서를 볼 수 있습니다.
## Try it
kind: cli
```sh
pwd
cat data.txt
printf '오류 예시\\n' >&2
```
```sh
printf '완료\\n'
exit 7
```
## Expect
보고서와 완료가 표시됩니다.
"""

WEB = """## What changed
새 인사 화면입니다.
## Try it
kind: web
## Expect
인사가 표시됩니다.
## Web
start: python app.py --host 0.0.0.0
port: 8765
path: /hello
steps:
- 인사 버튼 클릭
"""


@pytest.fixture
def ctx(git_history, tmp_path):
    git_history.update({"data.txt": "머지된 보고서\n"})
    git = GitRepo(git_history.clone)
    git.fetch("origin", "main")
    now = BASE_TIME + timedelta(hours=1)
    return InspectContext(
        FakeTracker(strict=True, now=lambda: now), StateStore(tmp_path / "state"),
        "owner/repo", Config(language="ko"), lambda: now, git_history.clone, git,
    )


def merged(ctx, number=1, card=CARD):
    pr = ctx.tracker.create_pr(
        head=f"foragent/issue-{number}", base="main", title="보고서", body="## Try it\n\n" + card,
    )
    ctx.tracker.merge_pr(pr, expected_head_sha=ctx.tracker.pr_head_sha(pr))
    return pr


def no_github_writes(ctx, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("local try operations must never write to GitHub")
    for method in (
        "comment", "create_issue", "edit_body", "add_labels", "remove_labels", "ensure_labels",
        "close_issue", "create_pr", "update_pr", "close_pr", "merge_pr",
    ):
        monkeypatch.setattr(ctx.tracker, method, fail)


def test_prepare_worktree_results_idempotence_and_changed_base(ctx, git_history, monkeypatch):
    merged(ctx)
    merged(ctx, 2, WEB)
    no_github_writes(ctx, monkeypatch)
    # Uncommitted local edits must never be used by prepared commands.
    local = ctx.repo_root / "data.txt"
    local.write_text("사용자의 로컬 변경\n")
    assert runtime.prepare(ctx) == [1]
    item = next(item for item in collect(ctx).items if item.number == 1)
    assert item.try_card == parse_try(CARD)
    prepared = item.prepared
    assert prepared.base_sha == git_command(ctx.repo_root, "rev-parse", "origin/main")
    assert prepared.results[0].exit_code == 0
    assert str(ctx.store.root / "inspect/wt") in prepared.results[0].output
    assert "머지된 보고서" in prepared.results[0].output
    assert "오류 예시" not in prepared.results[0].output
    assert prepared.results[0].stderr == "오류 예시\n"
    assert (prepared.results[1].output, prepared.results[1].exit_code) == ("완료\n", 7)
    assert local.read_text() == "사용자의 로컬 변경\n"
    assert not list((ctx.store.root / "inspect/wt").iterdir())
    assert "inspect/wt" not in git_command(ctx.repo_root, "worktree", "list")
    path = ctx.store.root / "inspect/prepared/1.json"
    stamp = path.stat().st_mtime_ns
    assert runtime.prepare(ctx) == [] and path.stat().st_mtime_ns == stamp
    git_history.update({"data.txt": "다음 보고서\n"})
    git_command(ctx.repo_root, "config", "remote.origin.fetch", "+refs/heads/unrelated:refs/remotes/origin/unrelated")
    assert runtime.prepare(ctx) == [1]
    updated = next(item for item in collect(ctx).items if item.number == 1).prepared
    assert updated.base_sha != prepared.base_sha
    assert "다음 보고서" in updated.results[0].output


def test_serve_fetches_explicit_base_ref_before_creating_worktree(ctx, git_history, monkeypatch):
    merged(ctx, card=WEB)
    git_history.update({"data.txt": "new remote base\n"})
    git_command(ctx.repo_root, "config", "remote.origin.fetch", "+refs/heads/unrelated:refs/remotes/origin/unrelated")
    monkeypatch.setattr(runtime, "_port_open", lambda port: False)
    calls = []
    run = ctx.git._run

    def record(args):
        calls.append(args)
        return run(args)

    monkeypatch.setattr(ctx.git, "_run", record)

    def launcher(argv, **kwargs):
        assert (kwargs["cwd"] / "data.txt").read_text() == "new remote base\n"
        return SimpleNamespace(pid=5001)

    runtime.serve(ctx, 1, process_launcher=launcher, port_waiter=lambda *args: True, hostname=lambda: None)
    fetch = ["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"]
    assert fetch in calls
    assert calls.index(fetch) < next(i for i, args in enumerate(calls) if args[:2] == ["worktree", "add"])
    runtime.stop(ctx, killer=lambda pid: None)


def test_prepare_scrubs_runner_environment_and_bounds_separate_output(ctx, monkeypatch):
    gate_bin = str(Path(__file__).resolve().parents[1] / ".venv/bin")
    for key in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        monkeypatch.setenv(key, "gate-environment")
    monkeypatch.setenv("PATH", gate_bin + os.pathsep + os.environ["PATH"])
    command = "\n".join([
        'test -z "${VIRTUAL_ENV}${VIRTUAL_ENV_PROMPT}${PYTHONHOME}${UV_PROJECT_ENVIRONMENT}" || exit 3',
        f'case ":$PATH:" in *":{gate_bin}:"*) exit 4;; esac',
        "printf '%3500s' ''", "printf '끝\\n' >&2",
    ])
    merged(ctx, card=f"## Try it\nkind: cli\n```sh\n{command}\n```\n")
    runtime.prepare(ctx)
    result = collect(ctx).items[0].prepared.results[0]
    assert result.exit_code == 0 and not result.timed_out
    assert len(result.output) == 3000 and result.output == " " * 3000
    assert result.stderr == "끝\n"


def test_prepare_timeout_uses_60_seconds_and_kills_child_group(ctx, monkeypatch):
    merged(ctx, card="## Try it\n```sh\nprintf 'started\\n'; sleep 30\n```\n")
    run = runtime.run_logged
    calls = []

    def fast_timeout(argv, **kwargs):
        calls.append((argv, kwargs["timeout_s"]))
        return run(argv, **{**kwargs, "timeout_s": 0.2})

    monkeypatch.setattr(runtime, "run_logged", fast_timeout)
    assert runtime.prepare(ctx) == [1]
    result = collect(ctx).items[0].prepared.results[0]
    assert calls == [(["/bin/sh", "-c", "printf 'started\\n'; sleep 30"], 60)]
    assert result.timed_out and result.exit_code != 0 and "started" in result.output
    assert not list((ctx.store.root / "inspect/wt").iterdir())


@pytest.mark.parametrize("exit_code", [0, 7])
def test_prepare_terminates_background_children_before_removing_worktree(ctx, tmp_path, monkeypatch, exit_code):
    pid_file = tmp_path / "background.pid"
    command = f'sleep 300 &\nprintf "%s" "$!" > {shlex.quote(str(pid_file))}\nexit {exit_code}'
    merged(ctx, card=f"## Try it\nkind: cli\n```sh\n{command}\n```\n")
    remove = ctx.git.remove_worktree
    removals = []

    def remove_after_exit(worktree):
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        removals.append(worktree)
        remove(worktree)

    monkeypatch.setattr(ctx.git, "remove_worktree", remove_after_exit)
    try:
        assert runtime.prepare(ctx) == [1]
        result = collect(ctx).items[0].prepared.results[0]
        assert result.exit_code == exit_code and not result.timed_out
        runtime.stop(ctx)
        assert len(removals) == 1
        assert not list((ctx.store.root / "inspect/wt").iterdir())
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_prepare_cleans_up_on_command_error_and_does_not_cache(ctx, monkeypatch):
    merged(ctx)
    def fail(*args):
        raise OSError("cannot launch")
    monkeypatch.setattr(runtime, "run_command", fail)
    with pytest.raises(OSError, match="cannot launch"):
        runtime.prepare(ctx)
    assert not (ctx.store.root / "inspect/prepared/1.json").exists()
    assert "inspect/wt" not in git_command(ctx.repo_root, "worktree", "list")


def test_store_card_precedes_pr_fallback_and_missing_card_is_optional(ctx):
    merged(ctx)
    merged(ctx, 2, "_not provided by the implementer_")
    saved = ctx.store.root / "try/1.md"
    saved.parent.mkdir(parents=True)
    saved.write_text(WEB)
    items = collect(ctx).items
    assert items[0].try_card == parse_try(WEB)
    assert items[1].try_card is None and items[1].prepared is None
    assert runtime.prepare(ctx) == []


def test_pr_try_instructions_without_embedded_heading_are_parsed(ctx):
    merged(ctx, card="kind: cli\n```sh\necho 보고서\n```\n## Expect\n보고서")
    card = collect(ctx).items[0].try_card
    assert card.commands == ["echo 보고서"] and card.expect == "보고서"


def test_inbox_summary_json_and_rendering(ctx):
    merged(ctx, card=CARD + "## Needs a human check\n- [ ] 화면 확인\n")
    for number, reason in ((10, Reason.SPEC_QUESTION), (11, Reason.HASH_MISMATCH),
                           (12, Reason.RISK_HIGH), (13, Reason.TIMEOUT)):
        ctx.tracker.add_issue(number, labels=["needs-human"])
        ctx.tracker.comment(number, format_comment(reason, "Summary"))
    runtime.prepare(ctx)
    inbox = collect(ctx)
    payload = json.loads(json.dumps(asdict(inbox)))
    assert payload["language"] == "ko"
    assert payload["summary"] == {"counts": inbox.counts, "estimated_minutes": 8}
    item = next(item for item in inbox.items if item.group == "try")
    assert next(item for item in payload["items"] if item["group"] == "try")["try_card"]["what_changed"] == "보고서를 볼 수 있습니다."
    assert "Decision 1 (~1 min)" in counts_line(inbox, detailed=True)
    assert "Try 1 (prepared)" in counts_line(inbox, detailed=True)
    rendered = "\n".join(try_lines(item))
    assert all(part in rendered for part in (
        "What changed:", "보고서를 볼 수 있습니다.", "Try it:", "cat data.txt",
        "Expect:", "보고서와 완료", "머지된 보고서", "exit_code=7",
    ))
    assert "not prepared" in try_lines(replace(item, prepared=None))


@pytest.mark.parametrize("available", [False, True])
def test_serve_with_injected_launcher_waiter_and_stop_all(ctx, monkeypatch, available):
    merged(ctx, card=WEB)
    merged(ctx, 2, WEB.replace("8765", "8766"))
    no_github_writes(ctx, monkeypatch)
    monkeypatch.setattr(runtime, "_port_open", lambda port: False)
    calls, waits, kills = [], [], []

    def launcher(argv, **kwargs):
        calls.append((argv, kwargs))
        assert (kwargs["cwd"] / "data.txt").read_text() == "머지된 보고서\n"
        assert kwargs["env"]["PORT"] in {"8765", "8766"}
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.STDOUT
        return SimpleNamespace(pid=5000 + len(calls))

    def waiter(port, timeout):
        waits.append((port, timeout))
        return True

    for number, port in ((1, 8765), (2, 8766)):
        urls = runtime.serve(
            ctx, number, process_launcher=launcher, port_waiter=waiter,
            hostname=lambda: "mini.tailnet.ts.net" if available else None,
        )
        assert urls == [f"http://localhost:{port}/hello"] + (
            [f"http://mini.tailnet.ts.net:{port}/hello"] if available else []
        )
    assert waits == [(8765, 90), (8766, 90)]
    assert all(call[0] == ["/bin/sh", "-c", "python app.py --host 0.0.0.0"] for call in calls)
    records = ctx.store.root / "inspect/served.json"
    assert json.loads(records.read_text()) == {"1": {"pid": 5001, "port": 8765}, "2": {"pid": 5002, "port": 8766}}
    runtime.stop(ctx, killer=kills.append)
    runtime.stop(ctx, killer=kills.append)
    assert kills == [5001, 5002] and json.loads(records.read_text()) == {}
    assert not (ctx.store.root / "inspect/serve-1").exists()
    assert "inspect/serve" not in git_command(ctx.repo_root, "worktree", "list")


@pytest.mark.parametrize("failure", ["launch", "timeout", "waiter"])
def test_serve_failure_cleans_up_worktree_and_process(ctx, monkeypatch, failure):
    merged(ctx, card=WEB)
    monkeypatch.setattr(runtime, "_port_open", lambda port: False)
    kills = []
    def launcher(*args, **kwargs):
        if failure == "launch":
            raise OSError("failed launch")
        return SimpleNamespace(pid=5001)
    def waiter(*args):
        if failure == "waiter":
            raise OSError("failed waiter")
        return False
    with pytest.raises((OSError, runtime.InspectRuntimeError)):
        runtime.serve(ctx, 1, process_launcher=launcher, port_waiter=waiter, killer=kills.append)
    assert kills == ([] if failure == "launch" else [5001])
    assert not (ctx.store.root / "inspect/serve-1").exists()
    assert runtime._served(ctx) == {}


def test_mark_seen_stops_served_apps(ctx, monkeypatch):
    merged(ctx, card=WEB)
    monkeypatch.setattr(runtime, "_port_open", lambda port: False)
    runtime.serve(ctx, 1, process_launcher=lambda *a, **k: SimpleNamespace(pid=5001),
                  port_waiter=lambda *a: True, hostname=lambda: None)
    kills = []
    monkeypatch.setattr(runtime, "kill_process_group", kills.append)
    assert inspect_actions.mark_seen(ctx) == ctx.now().isoformat()
    assert kills == [5001] and runtime._served(ctx) == {}
    assert not (ctx.store.root / "inspect/serve-1").exists()


def _preview_operation(repo_root, store_root, operation, number, connection, pause):
    ctx = InspectContext(FakeTracker(strict=True, now=lambda: BASE_TIME), StateStore(store_root),
                         "owner/repo", Config(), lambda: BASE_TIME, repo_root, GitRepo(repo_root))
    merged(ctx, card=WEB)
    merged(ctx, 2, WEB.replace("8765", "8766"))
    runtime._port_open = lambda port: False
    killed = []

    def paused():
        if pause:
            connection.send("paused")
            if not connection.poll(10):
                raise TimeoutError("preview operation was not resumed")
            assert connection.recv() == "continue"

    def launcher(*args, **kwargs):
        paused()
        return SimpleNamespace(pid=5000 + number)

    def killer(pid):
        paused()
        killed.append(pid)

    try:
        connection.send("started")
        if operation == "serve":
            runtime.serve(ctx, number, process_launcher=launcher, port_waiter=lambda *args: True,
                          hostname=lambda: None, killer=killer)
        else:
            runtime.stop(ctx, killer=killer)
        connection.send(killed)
    finally:
        connection.close()


@pytest.fixture
def preview_process(ctx):
    context = multiprocessing.get_context("spawn")
    children = []

    def start(operation, number=1, *, pause=False):
        parent, child = context.Pipe()
        process = context.Process(target=_preview_operation,
                                  args=(ctx.repo_root, ctx.store.root, operation, number, child, pause))
        process.start()
        child.close()
        children.append((process, parent))
        assert parent.poll(10) and parent.recv() == "started"
        return parent

    yield start
    for process, connection in children:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        connection.close()
        assert process.exitcode == 0
    runtime.stop(ctx, killer=lambda pid: None)


@pytest.mark.parametrize("first_operation,second_operation", [
    ("serve", "serve"), ("serve", "stop"), ("stop", "serve"), ("stop", "stop"),
])
def test_preview_registry_operations_are_serialized_across_processes(
    ctx, monkeypatch, preview_process, first_operation, second_operation,
):
    if first_operation == "stop":
        merged(ctx, card=WEB)
        monkeypatch.setattr(runtime, "_port_open", lambda port: False)
        runtime.serve(ctx, 1, process_launcher=lambda *args, **kwargs: SimpleNamespace(pid=5001),
                      port_waiter=lambda *args: True, hostname=lambda: None)
    first = preview_process(first_operation, pause=True)
    assert first.poll(10) and first.recv() == "paused"
    second = preview_process(second_operation, number=2)
    try:
        assert not second.poll(0.2)
    finally:
        first.send("continue")
    assert first.poll(10)
    assert first.recv() == ([5001] if first_operation == "stop" else [])
    assert second.poll(10)
    assert second.recv() == ([5001] if (first_operation, second_operation) == ("serve", "stop") else [])
    expected = {"2": {"pid": 5002, "port": 8766}} if second_operation == "serve" else {}
    if first_operation == second_operation == "serve":
        expected["1"] = {"pid": 5001, "port": 8765}
    assert runtime._served(ctx) == expected
    killed = []
    runtime.stop(ctx, killer=killed.append)
    assert sorted(killed) == sorted(record["pid"] for record in expected.values())
    assert runtime._served(ctx) == {}
    assert not list((ctx.store.root / "inspect").glob("serve-*/"))
    assert "inspect/serve-" not in git_command(ctx.repo_root, "worktree", "list")


@pytest.mark.parametrize("result", [
    '{"Self":{"DNSName":"mini.tailnet.ts.net."}}', '{}', 'bad json',
    FileNotFoundError("no tailscale"), subprocess.TimeoutExpired("tailscale", 5),
])
def test_tailscale_url_lookup_is_optional(monkeypatch, result):
    def run(argv, **kwargs):
        assert argv == ["tailscale", "status", "--json"] and kwargs["timeout"] == 5
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(stdout=result)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    assert runtime.tailscale_name() == ("mini.tailnet.ts.net" if isinstance(result, str) and "DNSName" in result else None)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_prepared_stdout_stderr_storage_and_public_rendering(ctx, monkeypatch, exit_code):
    from foragent.inspect_data import inbox_payload

    # These commands have no background children to terminate.
    monkeypatch.setattr("foragent.processes.kill_process_group", lambda pid: None)
    command = f"printf 'visible stdout'; printf 'hidden-prefix%1600s' '' >&2; printf 'error-tail' >&2; exit {exit_code}"
    merged(ctx, card=f"## Try it\nkind: cli\n```sh\n{command}\n```\n")
    assert runtime.prepare(ctx) == [1]
    stored = json.loads((ctx.store.root / "inspect/prepared/1.json").read_text())["results"][0]
    assert stored["output"] == "visible stdout"
    assert stored["stderr"].endswith("error-tail") and len(stored["stderr"]) == 1500
    inbox = collect(ctx)
    result = inbox_payload(inbox)["items"][0]["prepared"]["results"][0]
    assert result["output"] == "visible stdout"
    lines = try_lines(inbox.items[0])
    rendered = "\n".join(lines[lines.index("visible stdout"):])
    assert "visible stdout" in rendered
    if exit_code == 0:
        assert "stderr" not in result and "error-tail" not in rendered
    else:
        assert result["stderr"] == stored["stderr"]
        assert "error-tail" in rendered and "hidden-prefix" not in rendered
