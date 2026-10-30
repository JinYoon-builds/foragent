"""Public notification delivery: actual outbox routing and personal decision policy."""

import json
from pathlib import Path
import subprocess

import pytest
import yaml

from foragent import cli, notify
from foragent.config import Config
from foragent.state import StateStore
from test_skills_autosync import cli_exit, isolated_home, write_global_config  # noqa: F401


def records(repo):
    path = StateStore.for_repo(repo).root / "push-outbox.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("remote", ["git@github.com:offline/notify.git", "https://github.com/offline/notify.git"])
@pytest.mark.parametrize("linked", [False, True])
def test_shared_notify_entry_resolves_origin_without_config_repo(
    git_history, tmp_path, monkeypatch, remote, linked,
):
    root = git_history.clone
    subprocess.run(["git", "-C", str(root), "config", "remote.origin.url", remote], check=True)
    if linked:
        common = root / ".git"
        gitdir = common / "worktrees/offline-linked"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n")
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
        root = tmp_path / "linked"
        root.mkdir()
        (root / ".git").write_text(f"gitdir: {gitdir}\n")
    folder = root / "nested"
    folder.mkdir()
    monkeypatch.chdir(folder)
    assert notify.send(Config(repo=None, notify="claude-push"), "Origin", "Opaque Ζ42", None) is True
    assert records("offline/notify") == [{"title": "Origin", "line": "Opaque Ζ42", "link": None}]
    assert notify.send(Config(repo="offline/explicit", notify="claude-push"), "Explicit", "Ω89") is True
    assert [row["title"] for row in records("offline/explicit")] == ["Explicit"]
    assert len(records("offline/notify")) == 1, "Explicit repository remains authoritative"


def test_notify_without_origin_reports_failure_and_none_remains_off(tmp_path, monkeypatch, capsys):
    root = tmp_path / "no-origin"
    root.mkdir()
    (root / ".foragent.yml").write_text("skill_libraries: []\nnotify: claude-push\n")
    monkeypatch.chdir(root)
    assert notify.send(Config(notify="claude-push"), "Undelivered", "Opaque") is False
    assert cli_exit(cli.main, ["notify", "--title", "Undelivered", "--line", "Opaque",
                               "--repo-root", str(root)]) != 0
    assert capsys.readouterr().err.strip(), "The CLI cannot silently claim delivery"
    assert notify.send(Config(notify="none"), "Off", "Opaque") is True
    assert not records("offline/notify")


def test_public_notify_cannot_report_success_after_outbox_write_failure(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: offline/write-failure\nskill_libraries: []\nnotify: claude-push\n")
    monkeypatch.chdir(root)
    state = StateStore.for_repo("offline/write-failure").root
    state.mkdir(parents=True)
    (state / "push-outbox.jsonl").mkdir()  # Deterministic failure, even with privileged filesystem access.
    code = cli_exit(cli.main, ["notify", "--title", "Failed", "--line", "Opaque", "--repo-root", str(root)])
    assert code != 0, "A failed outbox write cannot be reported as a successful CLI delivery"
    assert capsys.readouterr().err.strip(), "Delivery failure must be visible without reading logs"


@pytest.mark.parametrize("policy", ["decisions", "all"])
def test_personal_filter_controls_public_notify_outbox_without_reading_meaning(
    tmp_path, monkeypatch, capsys, policy,
):
    root = tmp_path / "project"
    root.mkdir()
    # A project cannot turn a person's noise filter off.
    (root / ".foragent.yml").write_text(
        "repo: offline/policy\nskill_libraries: []\nnotify: claude-push\nnotify_filter: all\n")
    global_file = write_global_config({"skill_libraries": []})
    monkeypatch.chdir(root)
    assert cli_exit(cli.main, ["settings", "set", "notify_filter", policy,
                               "--repo-root", str(root), "--json"]) == 0
    setting = json.loads(capsys.readouterr().out)
    assert setting["scope"] == "me"
    assert yaml.safe_load(global_file.read_text())["notify_filter"] == policy
    kinds = ["question", "merge", "order", "blocked", "orchestrator-stopped", "silence", "summary", "status"]
    for kind in kinds:
        # Identical words intentionally provide no clue to the desired route.
        assert cli_exit(cli.main, ["notify", "--kind", kind, "--title", kind,
                                   "--line", "approval blocked decide silence Ω89",
                                   "--repo-root", str(root)]) == 0
    assert cli_exit(cli.main, ["notify", "--title", "untyped", "--line", "Decision needed",
                               "--repo-root", str(root)]) == 0
    expected = kinds[:3] if policy == "decisions" else [*kinds, "untyped"]
    assert [row["title"] for row in records("offline/policy")] == expected
    (root / ".foragent.yml").write_text("repo: offline/policy\nskill_libraries: []\nnotify: none\n")
    assert cli_exit(cli.main, ["notify", "--kind", "question", "--title", "Off", "--line", "Opaque",
                               "--repo-root", str(root)]) == 0
    assert [row["title"] for row in records("offline/policy")] == expected


def test_omitted_personal_filter_preserves_untyped_notification_delivery(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".foragent.yml").write_text("repo: offline/compat\nskill_libraries: []\nnotify: claude-push\n")
    monkeypatch.chdir(root)
    assert cli_exit(cli.main, ["notify", "--title", "Legacy", "--line", "Opaque"]) == 0
    assert [row["title"] for row in records("offline/compat")] == ["Legacy"]


# Existing installed hook fixtures exercise the #1026 -> notification contract.
from test_fa_report_since import report_case  # noqa: E402, F401
from test_presence_session_start import session_case  # noqa: E402, F401
from test_presence_order_stall import prompt  # noqa: E402


@pytest.fixture
def planning_notify_case(report_case, request):
    (report_case.root / ".git/config").write_text(
        '[remote "origin"]\nurl = https://github.com/offline/reports.git\n')
    return request.getfixturevalue("session_case")


@pytest.mark.parametrize("kind", ["blocked", "orchestrator-stopped", "silence"])
def test_suppressed_status_reaches_planning_context_without_a_human_question(
    planning_notify_case, monkeypatch, kind,
):
    from foragent import orch_inbox

    case = planning_notify_case
    global_file = Path.home() / ".config/foragent/config.yml"
    values = yaml.safe_load(global_file.read_text())
    global_file.write_text(yaml.safe_dump({**values, "notify_filter": "decisions"}))
    questions = orch_inbox.questions(case.store)
    before = records("offline/reports")
    opaque = f"Ω89 technical event {kind}"
    assert cli_exit(cli.main, ["notify", "--kind", kind, "--title", kind, "--line", opaque,
                               "--repo-root", str(case.root)]) == 0
    assert records("offline/reports") == before
    # Fresh hook invocations read persisted facts; delivery does not need a live planning seat.
    for _ in range(2):
        assert opaque in prompt(case, monkeypatch)
    assert orch_inbox.questions(case.store) == questions, "Status is context, not a new human question"
