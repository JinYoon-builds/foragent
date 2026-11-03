from dataclasses import asdict, replace
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
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


def test_prepare_timeout_uses_60_seconds_and_kills_child_group(ctx, monkeypatch, fake_process_table):
    from process_fakes import install_popen

    command = "offline-timed-out-worker"

    def output(process):
        if process.calls == 1:
            process.kwargs["stdout"].write(b"started\n")

    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["/bin/sh", "-c", command],
                  timeout=True, children=(42001,), communicate=output)
    merged(ctx, card=f"## Try it\n```sh\n{command}\n```\n")
    run, calls = runtime.run_logged, []

    def record(argv, **kwargs):
        calls.append((argv, kwargs["timeout_s"]))
        return run(argv, **kwargs)

    monkeypatch.setattr(runtime, "run_logged", record)
    assert runtime.prepare(ctx) == [1]
    result = collect(ctx).items[0].prepared.results[0]
    assert calls == [(["/bin/sh", "-c", command], 60)]
    assert result.timed_out and result.exit_code != 0 and "started" in result.output
    assert not {42000, 42001} & fake_process_table.live
    assert not list((ctx.store.root / "inspect/wt").iterdir())


@pytest.mark.parametrize("exit_code", [0, 7])
def test_prepare_terminates_background_children_before_removing_worktree(ctx, tmp_path, monkeypatch, fake_process_table, exit_code):
    from process_fakes import install_popen

    command = "offline-background-worker"
    install_popen(monkeypatch, fake_process_table, matches=lambda argv: argv == ["/bin/sh", "-c", command],
                  exit_code=exit_code, children=(42001,))
    merged(ctx, card=f"## Try it\nkind: cli\n```sh\n{command}\n```\n")
    remove = ctx.git.remove_worktree
    removals = []

    def remove_after_exit(worktree):
        assert 42001 not in fake_process_table.live
        removals.append(worktree)
        remove(worktree)

    monkeypatch.setattr(ctx.git, "remove_worktree", remove_after_exit)
    assert runtime.prepare(ctx) == [1]
    result = collect(ctx).items[0].prepared.results[0]
    assert result.exit_code == exit_code and not result.timed_out
    runtime.stop(ctx)
    assert len(removals) == 1
    assert not list((ctx.store.root / "inspect/wt").iterdir())


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
    """A merged PR is published: its body owns the card and a saved local copy never stands in."""
    merged(ctx, card=WEB)
    merged(ctx, 2, "_not provided by the implementer_")
    pr = ctx.tracker.create_pr(head="foragent/issue-3", base="main", title="보고서", body="## Try it\n\n" + WEB)
    ctx.tracker.merge_pr(pr, expected_head_sha=ctx.tracker.pr_head_sha(pr))
    (ctx.store.root / "try").mkdir(parents=True)
    for number in (1, 2, 3):
        (ctx.store.root / f"try/{number}.md").write_text(CARD)
    items = {item.number: item for item in collect(ctx).items}
    assert items[1].try_card == parse_try(WEB)
    assert items[2].try_card is None and items[2].prepared is None
    assert items[3].try_card == parse_try(WEB)
    ctx.tracker.update_pr(pr, base="main", title="보고서", body="Closes #3\n")
    items = {item.number: item for item in collect(ctx).items}
    assert items[3].try_card is None and items[3].prepared is None
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
    assert inbox.counts["human-check"] == 0
    assert payload["summary"] == {"counts": inbox.counts, "estimated_minutes": 7}
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
    assert json.loads(records.read_text()) == {"1": {"pid": 5001, "port": 8765, "process_identities": []}, "2": {"pid": 5002, "port": 8766, "process_identities": []}}
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
    from foragent import processes

    processes.list_processes = lambda: ()  # The injected launcher has no real PID.
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
    expected = {"2": {"pid": 5002, "port": 8766, "process_identities": []}} if second_operation == "serve" else {}
    if first_operation == second_operation == "serve":
        expected["1"] = {"pid": 5001, "port": 8765, "process_identities": []}
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


ENV_CARD = """## Try it
kind: cli
```sh
printf '%s|%s|%s|%s|%s|%s\\n' "$HOME" "$XDG_STATE_HOME" "${{TMUX-unset}}" "${{FORAGENT_SESSION_ID-unset}}" "$FORAGENT_VIEW" "$UV_CACHE_DIR"
```
```sh
cat "$HOME/.config/foragent/config.yml"; echo {marker} > "$HOME/mark"
```
```sh
ls "$HOME"
```
## Expect
It runs.
"""


def test_each_card_runs_in_its_own_throwaway_home_with_the_persons_language(ctx, monkeypatch, tmp_path):
    real_config = Path.home() / ".config/foragent/config.yml"
    real_config.parent.mkdir(parents=True, exist_ok=True)
    real_config.write_text("language: ko\nnotify: none\n", encoding="utf-8")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setenv("FORAGENT_SESSION_ID", "live-session")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    merged(ctx, 1, ENV_CARD.format(marker="one"))
    merged(ctx, 2, ENV_CARD.format(marker="two"))
    assert runtime.prepare(ctx) == [1, 2]
    items = {item.number: item for item in collect(ctx).items}
    homes = []
    for number, marker in ((1, "one"), (2, "two")):
        echo, config, listing = items[number].prepared.results
        home, state, tmux, session, view, uv_cache = echo.output.strip().split("|")
        homes.append(home)
        assert Path(home) != Path.home() and Path(home).parent == ctx.store.root / "inspect/wt"
        assert state == f"{home}/.local/state"
        assert (tmux, session, view) == ("unset", "unset", "off")
        # The uv cache is not Foragent state: it stays the person's, so `uv run` works offline.
        assert uv_cache == str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv")
        # Only the person's language is copied in; the rest of the personal config is not.
        assert json.loads(config.output) == {"language": "ko"}
        # Commands of one card share one home: the second sees the first one's write.
        assert "mark" in listing.output.split()
    assert homes[0] != homes[1]
    assert not list((ctx.store.root / "inspect/wt").iterdir())
    assert real_config.read_text(encoding="utf-8") == "language: ko\nnotify: none\n"
    assert not (Path.home() / "mark").exists()


def test_run_logged_env_none_removes_an_inherited_variable(tmp_path, monkeypatch):
    from foragent.processes import run_logged
    monkeypatch.setenv("FORAGENT_SESSION_ID", "live-session")
    monkeypatch.setenv("KEEP_ME", "kept")
    code, timed_out, out = run_logged(
        ["/bin/sh", "-c", 'printf "%s|%s|%s" "${FORAGENT_SESSION_ID-unset}" "$KEEP_ME" "$ADDED"'],
        cwd=tmp_path, timeout_s=10, log_path=tmp_path / "log", capture_stdout=True,
        env={"FORAGENT_SESSION_ID": None, "ADDED": "yes"},
    )
    assert (code, timed_out, out) == (0, False, "unset|kept|yes")


FA = shlex.join([sys.executable, "-c", "import sys; from foragent.cli import main; sys.exit(main(sys.argv[1:]))"])


def card(*commands):
    return "## Try it\nkind: cli\n" + "".join(f"```sh\n{command}\n```\n" for command in commands) + "## Expect\nIt runs.\n"


def test_config_root_variables_cannot_lead_a_try_to_the_persons_real_profile(ctx, monkeypatch, tmp_path):
    """CLAUDE_CONFIG_DIR and its kind point past HOME; a try must not follow them to the real files."""
    from foragent import statusline
    profile = tmp_path / "claude-profile"
    profile.mkdir()
    settings_file = profile / "settings.json"
    settings_file.write_text(json.dumps(statusline.with_hook({}), indent=2), encoding="utf-8")
    before = settings_file.read_bytes()
    assert statusline.has_hook(json.loads(before))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    for name in ("CODEX_HOME", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL"):
        monkeypatch.setenv(name, str(tmp_path / name))
    names = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL")
    merged(ctx, 1, card(f"{FA} statusline --uninstall",
                        "printf '%s|' " + " ".join(f'"${{{name}-unset}}"' for name in names)))
    assert runtime.prepare(ctx) == [1]
    uninstall, echo = collect(ctx).items[0].prepared.results
    assert uninstall.exit_code == 0, uninstall.stderr
    assert settings_file.read_bytes() == before, "the person's real Claude settings must keep their hooks"
    assert echo.output == "unset|" * len(names)


@pytest.mark.parametrize("service", ["launchctl", "osascript"])
def test_shared_machine_services_refuse_inside_a_try(ctx, monkeypatch, tmp_path, service):
    """launchd's gui/<uid> jobs and AppleScript are the person's own whatever HOME says."""
    calls = tmp_path / "calls"
    real = tmp_path / "real-bin"
    real.mkdir()
    for name in ("launchctl", "osascript"):
        fake = real / name
        fake.write_text(f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(calls))}\n', encoding="utf-8")
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{real}{os.pathsep}{os.environ['PATH']}")
    # `fa gate uninstall` itself, with the repository named (the test origin is a local path).
    gate_uninstall = shlex.join([sys.executable, "-c", "from pathlib import Path; from foragent import launchd; "
                                 "print(launchd.Launchd(Path('.'), repo='owner/repo').uninstall())"])
    command = gate_uninstall if service == "launchctl" else "osascript -e 'display notification \"x\"'"
    merged(ctx, 1, card(command))
    assert runtime.prepare(ctx) == [1]
    [result] = collect(ctx).items[0].prepared.results
    assert result.exit_code != 0 and not result.timed_out
    assert f"{service} is not available" in result.stderr
    assert not calls.exists(), f"a try command must never reach the person's real {service}"
