from datetime import UTC, datetime, timedelta
import json
import os
import subprocess
import sys

import pytest

from foragent import __version__, cli, launchd
from foragent.config import Config, ConfigError
from foragent.git import GitError
from foragent.launcher import spawn_notify
from foragent.reasons import Reason, format_comment
from foragent.state import StateStore
from foragent.tracker import FakeTracker, TrackerError


NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)
BODY = "## Done when\n```sh\ntrue\n```\n"


class FakeGit:
    def __init__(self):
        self.fetches = []
        self.remotes = []

    def fetch(self, remote, ref):
        self.fetches.append((remote, ref))

    def remote_slug(self):
        self.remotes.append("origin")
        return "owner/repo"

    def blob_shas(self, ref, paths):
        return {path: "missing" for path in paths}

    def remove_worktree(self, path):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    git = FakeGit()
    tracker = FakeTracker(now=lambda: NOW, strict=True)
    store = StateStore(tmp_path / "state", now=lambda: NOW)
    (tmp_path / ".foragent.yml").write_text("approvers: [human]\n", encoding="utf-8")
    calls = {"roots": [], "repos": [], "stores": [], "launches": [], "notifications": []}

    def git_repo(root):
        calls["roots"].append(root)
        return git

    def gh_tracker(repo):
        calls["repos"].append(repo)
        return tracker

    def for_repo(repo):
        calls["stores"].append(repo)
        return store

    def spawn(root, selected_store):
        assert root == tmp_path and selected_store is store
        return lambda n: calls["launches"].append(n) or 1234

    monkeypatch.setattr(cli, "GitRepo", git_repo)
    monkeypatch.setattr(cli, "GhTracker", gh_tracker)
    monkeypatch.setattr(cli.StateStore, "for_repo", for_repo)
    monkeypatch.setattr(cli, "spawn_runner", spawn)
    monkeypatch.setattr(cli, "spawn_notify", lambda *args: calls["notifications"].append(args))
    monkeypatch.setattr(cli, "load_config", lambda root: Config(approvers=["human"]))
    monkeypatch.setattr(cli.launchd, "status", lambda root, **kwargs: launchd.LaunchdStatus(
        "dev.foragent.owner.repo", False, False,
    ))
    return tmp_path, git, tracker, store, calls


def add_approved(tracker, number=1):
    tracker.add_issue(number, f"Issue {number}", BODY)
    tracker.record_label(number, "approved", "human")


def test_tick_no_start_json_builds_context_and_promotes(app, capsys):
    root, git, tracker, store, calls = app
    add_approved(tracker)
    assert cli.main(["gate", "tick", "--repo-root", str(root), "--no-start", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"skipped": None, "actions": ["#1 approval snapshot saved", "#1 promoted to ready"]}
    assert git.fetches == [("origin", "main")]
    assert calls["roots"] == [root]
    assert calls["repos"] == calls["stores"] == ["owner/repo"]
    assert calls["launches"] == [] and store.active_runs() == []


def test_tick_starts_by_default_and_prints_plain_report(app, capsys):
    root, git, tracker, store, calls = app
    add_approved(tracker)
    assert cli.main(["gate", "tick", "--repo-root", str(root)]) == 0
    assert "#1 started running (pid=1234)" in capsys.readouterr().out
    assert calls["launches"] == [1] and store.get_run(1).pid == 1234


@pytest.mark.parametrize("json_output", [False, True])
def test_skipped_tick_succeeds(app, capsys, json_output):
    root, git, tracker, store, calls = app
    with store.tick_lock():
        args = ["gate", "tick", "--repo-root", str(root)] + (["--json"] if json_output else [])
        assert cli.main(args) == 0
    out = capsys.readouterr().out
    if json_output:
        assert json.loads(out) == {"skipped": "another tick is running", "actions": []}
    else:
        assert out == "another tick is running\n"
    assert git.fetches == [] and calls["launches"] == []


def test_config_repo_overrides_remote_slug_and_default_root_is_cwd(app, monkeypatch):
    root, git, tracker, store, calls = app
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="custom/project"))
    assert cli.main(["gate", "tick", "--no-start"]) == 0
    assert calls["roots"] == [root]
    assert calls["repos"] == ["custom/project"] and git.remotes == []


@pytest.mark.parametrize("json_output", [True, False])
def test_status_groups_latest_reason_active_runs_and_last_ten(app, capsys, json_output):
    root, git, tracker, store, calls = app
    for number, labels in (
        (5, []), (4, ["approved"]), (3, ["ready", "approved"]),
        (2, ["running", "ready", "approved"]), (1, ["needs-human", "running", "ready"]),
    ):
        tracker.add_issue(number, f"Issue {number}", BODY, labels)
    tracker.comment(1, format_comment(Reason.SPEC_QUESTION, "First question."))
    tracker._now = lambda: NOW + timedelta(minutes=1)
    tracker.comment(1, format_comment(Reason.HASH_MISMATCH, "Re-approve."))
    tracker.comment(1, "A later unrelated comment.")
    for number in range(10, 22):
        store.start_run(number, branch="old", worktree="old")
        store.finish_run(number, "not-implemented")
    store.start_run(2, branch="foragent/issue-2", worktree=store.worktree_path(2), pid=123)
    args = ["gate", "status", "--live", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        status = json.loads(output)
        assert status["launchd"] == {
            "label": "dev.foragent.owner.repo", "plist_present": False,
            "loaded": False, "last_exit_status": None,
        }
        assert status["issues"] == {
            "needs-human": [{"number": 1, "title": "Issue 1", "reason": "hash-mismatch"}],
            "running": [{"number": 2, "title": "Issue 2"}],
            "pr-open": [],
            "ready": [{"number": 3, "title": "Issue 3"}],
            "approved-waiting": [{"number": 4, "title": "Issue 4"}],
            "unapproved": [{"number": 5, "title": "Issue 5"}],
        }
        assert [run["number"] for run in status["active_runs"]] == [2]
        assert [run["number"] for run in status["history"]] == list(range(12, 22))
    else:
        assert "launchd: not loaded\n" in output
        assert "#1 Issue 1 [hash-mismatch]" in output
        assert "#2 foragent/issue-2 pid=123" in output
        assert "#10 not-implemented" not in output and "#21 not-implemented" in output
    assert git.fetches == [] and calls["launches"] == []


@pytest.mark.parametrize("json_output", [True, False])
@pytest.mark.parametrize("labels,group", [
    (["approved"], "pr-open"),
    (["approved", "ready"], "pr-open"),
    (["approved", "running"], "running"),
    (["approved", "needs-human"], "needs-human"),
])
def test_status_groups_issues_awaiting_pr_review(app, capsys, json_output, labels, group):
    root, git, tracker, store, calls = app
    tracker.add_issue(1, "Awaiting review", BODY, labels)
    tracker.create_pr(head="foragent/issue-1", base="main", title="PR", body="Closes #1")
    args = ["gate", "status", "--live", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        groups = json.loads(output)["issues"]
        assert [item["number"] for item in groups[group]] == [1]
        assert sum(len(items) for items in groups.values()) == 1
    else:
        prefix = "running:\n  🧰 Common:\n    " if group == "running" else f"{group}:\n  "
        assert f"{prefix}#1 Awaiting review" in output


@pytest.mark.parametrize("pid", [None, 123])
def test_run_issue_records_own_pid_and_runs_without_approval(app, monkeypatch, pid):
    root, git, tracker, store, calls = app
    add_approved(tracker)
    tracker.add_labels(1, ["running"])
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1), pid=pid)
    finish_run = store.finish_run
    calls = []

    def finish(number, **kwargs):
        calls.append(number)
        assert store.get_run(number).pid == os.getpid()
        return finish_run(number, **kwargs)

    monkeypatch.setattr(store, "finish_run", finish)
    assert cli.main(["gate", "run-issue", "1", "--repo-root", str(root)]) == 0
    assert calls == [1]
    assert store.active_runs() == []
    assert store.get_run(1).outcome == "no-approval"
    assert store.get_run(1).pid == os.getpid()
    assert store.read_history() == [store.get_run(1)]
    assert tracker.get_issue(1).labels == {"approved"}
    assert tracker.get_issue(1).body == BODY and tracker.get_issue(1).state == "open"


def test_missing_run_is_one_clear_error(app, capsys):
    root, *_ = app
    assert cli.main(["gate", "run-issue", "99", "--repo-root", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "foragent: no run record for issue #99\n"


@pytest.mark.parametrize("outcome,code", [("merged", 0), ("error", 1)])
def test_cli_dispatches_to_runner_and_returns_failure_code(app, monkeypatch, outcome, code):
    root, git, tracker, store, _ = app
    store.start_run(1, branch="foragent/issue-1", worktree=store.worktree_path(1))
    calls = []

    def runner(number, ctx):
        calls.append(number)
        assert ctx.repo_root == root and ctx.git is git and ctx.tracker._tracker is tracker
        assert ctx.store is store and store.get_run(number).pid == os.getpid()
        return outcome

    monkeypatch.setattr(cli, "run_issue", runner)
    assert cli.main(["gate", "run-issue", "1", "--repo-root", str(root)]) == code
    assert calls == [1]


@pytest.mark.parametrize("error", [
    ConfigError("invalid configuration\nmore details"),
    TrackerError("gh: authentication required\nHTTP 401"),
    GitError("fatal: cannot fetch origin"),
])
def test_error_exit_code_and_single_stderr_line(app, monkeypatch, capsys, error):
    root, git, tracker, store, calls = app

    def fail(*args):
        raise error

    if isinstance(error, ConfigError):
        monkeypatch.setattr(cli, "load_config", fail)
    elif isinstance(error, TrackerError):
        monkeypatch.setattr(tracker, "list_issues", fail)
    else:
        monkeypatch.setattr(git, "fetch", fail)
    assert cli.main(["gate", "tick", "--repo-root", str(root), "--no-start"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert captured.err.startswith("foragent: ")


def test_module_entry_point_version():
    result = subprocess.run(
        [sys.executable, "-m", "foragent.cli", "--version"], check=True,
        capture_output=True, text=True,
    )
    from foragent._build import GIT_SHA

    assert result.stdout == f"foragent {__version__} ({GIT_SHA})\n" and result.stderr == ""


def test_module_entry_point_configuration_error(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "foragent.cli", "gate", "tick", "--repo-root", str(tmp_path), "--no-start"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert result.stdout == "" and "not a Foragent repo" in result.stderr


def test_init_cli_passes_all_flags_and_prints_summary(tmp_path, monkeypatch, capsys):
    calls = []

    class Report:
        def summary(self):
            return "Setup summary"

    def init(root, **kwargs):
        calls.append((root, kwargs))
        return Report()

    monkeypatch.setattr(cli, "init_repo", init)
    assert cli.main([
        "init", "--repo-root", str(tmp_path), "--approver", "alice", "bob", "--approver", "carol",
        "--review", "careful", "--executor", "claude", "--run-hours", "22-08", "--language", "ko",
        "--no-push", "--no-launchd", "--no-board", "--dry-run",
    ]) == 0
    assert calls == [(tmp_path, {
        "approvers": ["alice", "bob", "carol"], "review": "careful", "executor": "claude",
        "run_hours": "22-08", "language": "ko", "no_push": True, "no_launchd": True, "no_board": True, "dry_run": True,
    })]
    assert capsys.readouterr().out == "Setup summary\n"


def test_init_cli_defaults_to_cwd_and_default_options(tmp_path, monkeypatch, capsys):
    from foragent.init import InitReport

    monkeypatch.chdir(tmp_path)

    def init(root, **kwargs):
        assert root == tmp_path
        assert kwargs == {
            "approvers": None, "review": None, "executor": None, "run_hours": None,
            "no_push": False, "no_launchd": False, "no_board": False, "dry_run": False, "language": None,
        }
        return InitReport([], [], False)

    monkeypatch.setattr(cli, "init_repo", init)
    assert cli.main(["init"]) == 0


def test_init_cli_keeps_explicit_default_options(tmp_path, monkeypatch):
    from foragent.init import InitReport

    def init(root, **kwargs):
        assert kwargs["executor"] == "codex" and kwargs["run_hours"] == "always"
        assert kwargs["review"] == "batch" and kwargs["approvers"] == ["human"]
        return InitReport([], [], False)

    monkeypatch.setattr(cli, "init_repo", init)
    assert cli.main([
        "init", "--repo-root", str(tmp_path), "--executor", "codex", "--run-hours", "always",
        "--review", "batch", "--approver", "human",
    ]) == 0


@pytest.mark.parametrize("dry_run", [False, True])
def test_init_view_uses_normalized_root_and_skips_dry_run(app, monkeypatch, dry_run):
    from foragent.init import InitReport

    root, git, tracker, store, calls = app
    nested = root / "nested"
    nested.mkdir()
    opened = []
    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.setattr(cli, "init_repo", lambda *a, **kw: InitReport(
        [], [], dry_run, normalized_root=root,
    ))
    monkeypatch.setattr(cli, "load_config", lambda selected: (
        opened.append(selected) or Config(repo="custom/repo", language="ko")
    ))
    monkeypatch.setattr(cli.watch_open, "open_view", lambda *args: opened.append(args))
    argv = ["init", "--repo-root", str(nested)] + (["--dry-run"] if dry_run else [])
    assert cli.main(argv) == 0
    assert opened == ([] if dry_run else [root, (root, store, "ko")])
    assert calls["stores"] == ([] if dry_run else ["custom/repo"])
    assert git.remotes == []


def test_init_view_metadata_failure_keeps_success(app, monkeypatch, capsys):
    from foragent.init import InitReport

    root, git, *_ = app
    monkeypatch.delenv("FORAGENT_VIEW")
    monkeypatch.setattr(cli, "init_repo", lambda *a, **kw: InitReport([], [], False))
    monkeypatch.setattr(cli, "load_config", lambda root: Config(language="ko"))

    def unavailable():
        raise GitError("origin unavailable\ntry again")

    monkeypatch.setattr(git, "remote_slug", unavailable)
    assert cli.main(["init", "--repo-root", str(root)]) == 0
    output = capsys.readouterr()
    assert "Foragent setup:" in output.out
    assert output.err == "foragent: 레인 화면을 열지 못했어요(origin unavailable try again). 다시 시도해요\n"


@pytest.mark.parametrize("command", ["publish", "orch"])
@pytest.mark.parametrize("opt_out", [False, True])
def test_automatic_view_failure_keeps_command_success(app, monkeypatch, capsys, command, opt_out):
    root, _, tracker, store, _ = app
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="owner/repo", language="ko"))
    if opt_out:
        monkeypatch.setenv("FORAGENT_VIEW", "off")
    else:
        monkeypatch.delenv("FORAGENT_VIEW")
    opened = []

    def unavailable(*args):
        opened.append(args)
        raise OSError("terminal unavailable\ntry again")

    monkeypatch.setattr(cli.watch_open, "open_view", unavailable)
    if command == "publish":
        plan = root / "plan.json"
        plan.write_text("{}")
        monkeypatch.setattr(cli.order, "publish", lambda *a, **kw: cli.order.PublishReport(
            "View task", 1, [], [], False,
        ))
        argv = ["order", "publish", str(plan)]
    else:
        issue = tracker.add_issue(1, "Waiting for repair", labels=["blocked"])
        store.record_issue(issue, {})
        argv = ["orch", "watch"]

    assert cli.main([*argv, "--repo-root", str(root)]) == 0
    output = capsys.readouterr()
    assert opened == ([] if opt_out else [(root, store, "ko")])
    assert output.err == ("" if opt_out else
                          "foragent: 레인 화면을 열지 못했어요(terminal unavailable try again). 다시 시도해요\n")
    if command == "publish":
        assert output.out.startswith("Published: View task\nParent: #1\n")
    else:
        assert json.loads(output.out)["blocked"] == [1]
        assert json.loads((store.root / "orch-watch.json").read_text())["blocked"] == [1]


@pytest.mark.parametrize("json_output", [False, True])
def test_status_uses_configured_repo_without_origin(app, monkeypatch, capsys, json_output):
    from foragent.config import load_config
    from foragent.git import GitRepo

    root, git, tracker, store, calls = app
    (root / ".foragent.yml").write_text("repo: custom/project\n")
    monkeypatch.setattr(cli, "load_config", lambda root: load_config(root, root / "missing-global.yml"))
    monkeypatch.setattr(cli, "GitRepo", GitRepo)
    # Restore the real launchd status path, with an injected launchctl result.
    monkeypatch.setattr(launchd, "status", lambda root, **kwargs: launchd.Launchd(
        root, home=root, run=lambda args: subprocess.CompletedProcess(args, 113, stdout="", stderr=""),
        **kwargs,
    ).status())
    args = ["gate", "status", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert calls["repos"] == ["custom/project"]
    if json_output:
        assert json.loads(output)["launchd"]["label"] == launchd.label_for("custom/project")
    else:
        assert "launchd: not loaded\n" in output


@pytest.mark.parametrize("action", ["install", "uninstall", "launchd-status"])
def test_launchd_cli_needs_no_config_or_tracker(tmp_path, monkeypatch, capsys, action, kicks):
    monkeypatch.chdir(tmp_path)
    calls = []

    def operation(root):
        calls.append(root)
        if action == "launchd-status":
            return launchd.LaunchdStatus("dev.foragent.owner.repo", True, True, 42)
        return "dev.foragent.owner.repo"

    monkeypatch.setattr(cli.launchd, "status" if action == "launchd-status" else action, operation)
    assert cli.main(["gate", action]) == 0
    assert calls == [tmp_path]
    assert kicks == ([tmp_path] if action == "install" else [])
    output = capsys.readouterr().out
    assert "dev.foragent.owner.repo" in output
    if action == "launchd-status":
        assert "plist present, loaded, last exit status: 42" in output


def test_status_prints_loaded_launchd(app, monkeypatch, capsys):
    root, *_ = app
    monkeypatch.setattr(cli.launchd, "status", lambda root, **kw: launchd.LaunchdStatus("job", True, True))
    assert cli.main(["gate", "status", "--repo-root", str(root)]) == 0
    assert "launchd: loaded\n" in capsys.readouterr().out


def test_init_and_launchd_errors_are_clear(tmp_path, monkeypatch, capsys, kicks):
    def fail(*args, **kwargs):
        raise launchd.LaunchdError("launchctl unavailable\nmore details")

    monkeypatch.setattr(cli.launchd, "install", fail)
    assert cli.main(["gate", "install", "--repo-root", str(tmp_path)]) == 1
    assert capsys.readouterr().err == "foragent: launchctl unavailable more details\n"
    assert kicks == []


@pytest.mark.parametrize("json_output", [False, True])
def test_inspect_cli_text_and_json_shapes(app, capsys, json_output):
    root, git, tracker, store, calls = app
    tracker.add_issue(3, "Add tally report", "Original", ["needs-human"])
    details = "\n".join(f"Detail {i}" for i in range(16))
    tracker.comment(3, format_comment(Reason.SPEC_QUESTION, "Choose a format", details))
    args = ["inspect", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    if json_output:
        inbox = json.loads(captured.out)
        assert set(inbox) == {"items", "counts", "since", "summary", "language"}
        assert inbox["language"] == "en"
        assert inbox["summary"] == {"counts": inbox["counts"], "estimated_minutes": 1}
        assert inbox["counts"] == {
            "decision": 1, "reapprove": 0, "review": 0,
            "human-check": 0, "try": 0, "tech-failure": 0,
        }
        assert inbox["items"] == [{
            "group": "decision", "number": 3, "title": "Add tally report",
            "url": "https://github.com/owner/repo/issues/3", "reason": "spec-question",
            "summary": "Choose a format", "details": details, "pr": None, "answered": False,
            "try_card": None, "prepared": None, "lane": "main", "lane_display": "🧰 Common",
        }]
        assert datetime.fromisoformat(inbox["since"]).tzinfo is not None
    else:
        assert captured.out.splitlines() == [
            "Decision 1 (~1 min)", "[decision] 🧰 Common · #3 Add tally report — Choose a format",
            *(f"  Detail {i}" for i in range(12)), "  https://github.com/owner/repo/issues/3",
        ]
    assert git.fetches == [] and calls["launches"] == []
    assert not store.root.exists()


@pytest.mark.parametrize("json_output", [False, True])
def test_inspect_cli_try_is_latest_per_issue_with_missing_card(app, capsys, json_output):
    root, git, tracker, store, calls = app
    tracker.add_issue(7, "Tally report")
    older = tracker.create_pr(
        head="foragent/issue-7", base="main", title="Old tally", body="Old body\n" * 20,
    )
    tracker.merge_pr(older, expected_head_sha=tracker.pr_head_sha(older))
    latest = tracker.create_pr(
        head="foragent/issue-7", base="main", title="Latest tally", body="Latest body\n" * 20,
    )
    tracker._now = lambda: NOW + timedelta(minutes=1)
    tracker.merge_pr(latest, expected_head_sha=tracker.pr_head_sha(latest))
    store.update_inspect(since=(NOW - timedelta(days=1)).isoformat())
    args = ["inspect", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    url = f"https://github.com/owner/repo/pull/{latest}"
    summary = f"Merged in PR #{latest}; try it"
    if json_output:
        inbox = json.loads(captured.out)
        assert inbox["counts"]["try"] == 1
        assert inbox["items"] == [{
            "group": "try", "number": 7, "title": "Latest tally", "url": url,
            "reason": None, "summary": summary, "details": "", "pr": latest, "answered": False,
            "try_card": None, "prepared": None, "lane": "main", "lane_display": "🧰 Common",
        }]
    else:
        assert captured.out.splitlines() == [
            "Try 1 (not prepared)", f"[try] 🧰 Common · #7 Latest tally — {summary} {url}",
            "  What changed:", "  not provided", "  Try it:", "  not provided",
            "  Expect:", "  not provided", "  not prepared",
        ]
    assert git.fetches == [] and calls["launches"] == []


@pytest.mark.parametrize("json_output", [False, True])
def test_inspect_empty_output(app, capsys, json_output):
    root, *_ = app
    args = ["inspect", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        inbox = json.loads(output)
        assert inbox["items"] == [] and not any(inbox["counts"].values())
    else:
        assert output == "No items.\n"


@pytest.mark.parametrize("json_output", [False, True])
def test_inspect_renders_card_prepared_results_and_language(app, monkeypatch, capsys, json_output):
    root, git, tracker, store, _ = app
    monkeypatch.setattr(cli, "load_config", lambda root: Config(language="ko"))
    card = "## What changed\n합계 표시\n## Try it\nkind: cli\n```sh\necho 합계:3\n```\n## Expect\n합계:3"
    number = tracker.create_pr(head="foragent/issue-7", base="main", title="합계", body="## Try it\n\n" + card)
    tracker.merge_pr(number, expected_head_sha=tracker.pr_head_sha(number))
    saved = store.root / "inspect/prepared/7.json"
    saved.parent.mkdir(parents=True)
    saved.write_text(json.dumps({"base_sha": "abc123", "results": [
        {"command": "echo 합계:3", "output": "합계:3\n", "exit_code": 0, "timed_out": False},
    ]}))
    args = ["inspect", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        payload = json.loads(output)
        assert payload["language"] == "ko" and payload["summary"]["estimated_minutes"] == 1
        item, = payload["items"]
        assert item["try_card"]["commands"] == ["echo 합계:3"]
        assert item["prepared"]["results"][0]["output"] == "합계:3\n"
    else:
        assert output.startswith("Try 1 (prepared)\n")
        assert all(part in output for part in (
            "What changed:", "합계 표시", "Try it:", "echo 합계:3", "Expect:",
            "Prepared output (base abc123)", "합계:3", "exit_code=0",
        ))


@pytest.mark.parametrize("action", ["prepare", "serve", "stop"])
@pytest.mark.parametrize("root_before_action", [False, True])
def test_inspect_cli_local_actions_receive_repo_context(app, monkeypatch, capsys, action, root_before_action):
    root, git, tracker, store, _ = app
    calls = []
    def operation(ctx, *numbers):
        assert ctx.repo_root == root and ctx.git is git and ctx.store is store
        calls.append(numbers)
        return {"prepare": [7], "serve": ["http://localhost:8765/hello", "http://mini:8765/hello"], "stop": None}[action]
    monkeypatch.setattr(cli.inspect_actions, action, operation)
    root_args = ["--repo-root", str(root)]
    action_args = [action] + (["7"] if action == "serve" else [])
    args = ["inspect", *(root_args + action_args if root_before_action else action_args + root_args)]
    assert cli.main(args) == 0
    assert calls == ([(7,)] if action == "serve" else [()])
    assert capsys.readouterr().out == {
        "prepare": "Prepared: #7\n", "serve": "http://localhost:8765/hello\nhttp://mini:8765/hello\n",
        "stop": "Served apps stopped.\n",
    }[action]


def test_inspect_cli_records_answers_feedback_and_mark_seen(app, capsys, kicks):
    root, git, tracker, store, calls = app
    tracker.add_issue(1, "Question", BODY, ["needs-human"])
    tracker.comment(1, format_comment(Reason.SPEC_QUESTION, "Question"))
    for _ in range(2):
        assert cli.main(["inspect", "answer", "1", "--text", "A\nA note", "--repo-root", str(root)]) == 0
    assert tracker.list_comments(1)[-1].body == "<!-- foragent:decision -->\n**Decision:**\nA\nA note"
    assert len(tracker.list_comments(1)) == 2
    assert tracker.get_issue(1).labels == {"needs-human"} and tracker.get_issue(1).body == BODY
    args = ["inspect", "--repo-root", str(root), "feedback", "--title", "Wrong spacing", "--body", "Fix it", "--about", "1"]
    assert cli.main(args) == cli.main(args) == 0
    issue = tracker.get_issue(2)
    assert (issue.body, issue.labels) == ("Fix it\n\nAbout #1.", {"feedback"})
    before = datetime.now(UTC)
    assert cli.main(["inspect", "mark-seen", "--repo-root", str(root)]) == 0
    assert before <= datetime.fromisoformat(store.load_inspect().since) <= datetime.now(UTC)
    assert "Decision recorded:" in capsys.readouterr().out
    assert git.fetches == [] and calls["launches"] == []
    assert kicks == [root, root]


def test_inspect_cli_retry_and_refusal(app, capsys, kicks):
    root, git, tracker, store, _ = app
    tracker.add_issue(1, labels=["needs-human"])
    tracker.comment(1, format_comment(Reason.SPEC_QUESTION, "Question"))
    args = ["inspect", "retry", "1", "--repo-root", str(root)]
    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "not a tech-failure item; retry refused" in captured.err
    assert kicks == []
    tracker.comment(1, format_comment(Reason.TIMEOUT, "Too large"))
    assert cli.main(args) == cli.main(args) == 0
    assert tracker.get_issue(1).labels == frozenset()
    assert tracker.list_comments(1)[-1].body == "<!-- foragent:retry -->\nRetry requested."
    assert kicks == [root, root]


@pytest.mark.parametrize("reason", [Reason.RETRIES_EXCEEDED, Reason.TIMEOUT])
def test_inspect_retry_existing_failed_pr_can_be_scheduled_on_next_tick(app, monkeypatch, capsys, reason):
    root, git, tracker, store, _ = app
    monkeypatch.setattr(cli, "load_config", lambda root: Config(approvers=["human"], review="fast"))
    add_approved(tracker)
    tracker.add_labels(1, ["needs-human"])
    tracker.comment(1, format_comment(reason, "CI did not pass"))
    pr = tracker.create_pr(head="foragent/issue-1", base="main", title="Failed PR", body="Closes #1")
    assert cli.main(["inspect", "retry", "1", "--repo-root", str(root)]) == 0
    assert tracker.get_issue(pr).state == "closed"
    assert tracker.list_comments(pr)[-1].body == "Closed by Foragent retry"
    assert tracker.deleted_branches == ["foragent/issue-1"]
    capsys.readouterr()
    assert cli.main(["gate", "tick", "--no-start", "--repo-root", str(root), "--json"]) == 0
    assert "#1 promoted to ready" in json.loads(capsys.readouterr().out)["actions"]
    assert "ready" in tracker.get_issue(1).labels


def test_inspect_cli_notify_logs_failure_and_retries_then_dedupes(app, monkeypatch, capsys, caplog):
    root, git, tracker, store, _ = app
    tracker.add_issue(1, "Issue 1", labels=["needs-human"])
    tracker.comment(1, "<!-- foragent:needs-human reason=escalated -->\nChoose a format")
    monkeypatch.setattr(cli, "load_config", lambda root: Config(
        notify="command:/offline/notifier", notify_summary_hour=(datetime.now().astimezone().hour + 1) % 24,
    ))
    args = ["inspect", "notify", "--repo-root", str(root)]
    calls = []

    def fail(*args, **kwargs):
        raise FileNotFoundError("notifier unavailable")

    monkeypatch.setattr(subprocess, "run", fail)
    assert cli.main(args) == 0
    assert "notifier unavailable" in caplog.text
    assert capsys.readouterr().out == "#1 Issue 1 (escalated: Choose a format)\n"
    assert store.load_inspect().notified_signature is None
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert cli.main(args) == cli.main(args) == 0
    assert capsys.readouterr().out == "#1 Issue 1 (escalated: Choose a format)\n"
    assert len(calls) == 1
    assert calls[0][1]["env"]["FA_TITLE"] == "Foragent owner/repo"


def test_notify_cli_resolves_repo_without_github_access(app, monkeypatch):
    root, git, tracker, store, _ = app
    monkeypatch.setattr(cli, "GhTracker", lambda *args: pytest.fail("notification must not contact GitHub"))
    assert cli.main(["notify", "--repo-root", str(root), "--title", "확인", "--line", "준비 완료"]) == 0
    record = json.loads((store.root / "push-outbox.jsonl").read_text())
    assert record == {"title": "확인", "line": "준비 완료", "link": None}
    assert git.remotes == ["origin"]


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_tick_launches_notify_after_report_outside_lock_and_spawn_failure_is_isolated(
    app, monkeypatch, capsys, json_output, failure,
):
    root, git, tracker, store, _ = app
    add_approved(tracker)
    calls = []
    def popen(args, **kwargs):
        # tick() has returned and printed its report before the child is spawned.
        output = capsys.readouterr().out
        if json_output:
            assert json.loads(output)["actions"][-1] == "#1 promoted to ready"
        else:
            assert output.endswith("#1 promoted to ready\n")
        with store.tick_lock() as acquired:
            assert acquired
        calls.append((args, kwargs))
        if failure:
            raise OSError("cannot spawn\nextra details")
        kwargs["stdout"].write(b"foragent: notification delivery failed\n")
        return object()
    monkeypatch.setattr(cli, "spawn_notify", spawn_notify)
    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(cli.inspect_actions, "notify", lambda *args: pytest.fail("tick must not notify synchronously"))
    args = ["gate", "tick", "--no-start", "--repo-root", str(root)] + (["--json"] if json_output else [])
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert len(calls) == 1
    assert calls[0][0] == [
        sys.executable, "-m", "foragent.cli", "inspect", "notify", "--repo-root", str(root),
    ]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdout"] is calls[0][1]["stderr"]
    assert captured.out == ""
    if failure:
        assert captured.err == "foragent: inspect notify failed: cannot spawn extra details\n"
    else:
        assert captured.err == ""
        assert (store.root / "notify.log").read_text() == "foragent: notification delivery failed\n"


def test_tick_returns_while_notification_child_is_still_running(app, monkeypatch, capsys):
    root, git, tracker, store, _ = app
    tracker.add_issue(1, labels=["needs-human"])
    popen = subprocess.Popen
    children = []

    def slow_notification(args, **kwargs):
        # Replace GitHub/osascript with a real offline child that stays busy.
        child = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(cli, "spawn_notify", spawn_notify)
    monkeypatch.setattr(cli.subprocess, "Popen", slow_notification)
    try:
        assert cli.main(["gate", "tick", "--repo-root", str(root), "--no-start", "--json"]) == 0
        assert len(children) == 1 and children[0].poll() is None
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"skipped": None, "actions": []}
        assert captured.err == ""
        assert store.load_inspect().notified_signature is None
    finally:
        for child in children:
            child.terminate()
            child.wait(timeout=5)


def test_status_groups_running_and_ready_by_lane(app, capsys):
    root, git, tracker, store, calls = app
    tracker.add_issue(1, "Default ready", BODY, ["approved", "ready"])
    tracker.add_issue(2, "UI running", BODY, ["approved", "running", "lane:ui"])
    tracker.add_issue(3, "Data ready", BODY, ["approved", "ready", "lane:data"])
    tracker.add_issue(4, "Unknown ready", BODY, ["approved", "ready", "lane:missing"])
    store.start_run(2, branch="foragent/issue-2", worktree="wt", lane="ui")
    config = Config(lanes={"ui": {}, "data": {}})
    cli._refresh_status(tracker, store, config)
    status = cli._status(tracker, store, config)
    assert list(status["lanes"]) == ["data", "ui", "main"]
    assert [item["number"] for item in status["lanes"]["main"]["ready"]] == [1, 4]
    assert [item["number"] for item in status["lanes"]["data"]["ready"]] == [3]
    assert [item["number"] for item in status["lanes"]["ui"]["running"]] == [2]
    cli._print_status(status | {"launchd": {"loaded": False}})
    output = capsys.readouterr().out
    assert "running:\n  ui:\n    #2 UI running" in output
    assert "ready:\n  data:\n    #3 Data ready\n  🧰 Common:\n" in output


def test_orch_watch_follow_prints_only_changes_and_stops_cleanly(app, monkeypatch, capsys):
    from foragent import orch

    root, git, tracker, store, calls = app
    add_approved(tracker)
    store.record_issue(tracker.get_issue(1), {})
    output, polls = [], []

    def sleep(seconds):
        polls.append(seconds)
        lines = capsys.readouterr().out.splitlines()
        # An initial snapshot, a silent identical poll, then one changed snapshot.
        assert len(lines) == (0 if len(polls) == 2 else 1)
        output.extend(json.loads(line) for line in lines)
        if len(polls) == 2:
            store.record_labels(1, ["blocked"], remove=False, lanes={})
        if len(polls) == 3:
            raise KeyboardInterrupt
        assert len(polls) <= 3

    monkeypatch.setattr(orch.time, "sleep", sleep)
    assert cli.main(["orch", "watch", "--repo-root", str(root), "--interval", "0.01", "--follow"]) == 0
    assert polls == [0.01] * 3
    assert [snapshot["blocked"] for snapshot in output] == [[], [1]]
    assert json.loads((store.root / "orch-watch.json").read_text()) == output[-1]
    assert calls["launches"] == calls["notifications"] == git.fetches == []


@pytest.mark.parametrize("interval", ["0", "-1", "nan", "inf", "-inf"])
def test_orch_watch_refuses_invalid_intervals(app, capsys, interval):
    root, _, _, store, _ = app
    assert cli.main(["orch", "watch", "--repo-root", str(root), f"--interval={interval}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "finite positive" in captured.err
    assert not (store.root / "orch-watch.json").exists()
