import json
import subprocess

import pytest

from foragent import notify
from foragent.config import Config
from foragent.state import StateStore


@pytest.mark.parametrize("failure", [
    FileNotFoundError("missing executable"),
    subprocess.CalledProcessError(1, ["notifier"]),
    subprocess.TimeoutExpired(["notifier"], 10),
])
def test_process_failure_is_logged_and_returns_false(monkeypatch, caplog, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    assert notify.send(Config(notify="command:/notifier"), "Title", "Line") is False
    assert "Notification delivery failed" in caplog.text


def test_outbox_write_failure_is_isolated(tmp_path, monkeypatch, caplog):
    root = tmp_path / "state"
    root.write_text("not a directory")
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(root))
    assert notify.send(Config(repo="owner/repo"), "Title", "Line") is False
    assert caplog.records


def test_default_outbox_preserves_unicode_and_multiline_as_one_record(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    for _ in range(2):
        assert notify.send(Config(repo="owner/repo"), "검토", "첫 줄\n다음 줄") is True
    lines = (tmp_path / "push-outbox.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"title": "검토", "line": "첫 줄\n다음 줄", "link": None}


def test_command_runs_literal_path_and_clears_inherited_link(monkeypatch):
    calls = []
    monkeypatch.setenv("FA_LINK", "old link")
    monkeypatch.setenv("NOTIFY_TEST", "inherited")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert notify.send(Config(notify="command:/path with spaces/notify"), "Title", "Line")
    args, kwargs = calls[0]
    assert args == (["/path with spaces/notify"],)
    assert kwargs["env"]["FA_LINK"] == ""
    assert kwargs["env"]["NOTIFY_TEST"] == "inherited"
    assert kwargs["check"] is True and kwargs["timeout"] == 10


@pytest.mark.parametrize("target", ["unknown", "command:", "slack:", "ntfy:", {"old": "setting"}])
def test_invalid_destination_is_logged_without_raising(target, caplog):
    assert notify.send(Config(notify=target), "Title", "Line") is False
    assert caplog.records


def _journal(root):
    path = root / "orch-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("policy", ["decisions", "all"])
def test_filter_pushes_by_producer_kind_and_keeps_the_rest_as_notices(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    config = Config(repo="owner/repo", notify_filter=policy)
    for kind in (*notify.KINDS, None):
        with notify.sending(kind) as outcome:
            assert notify.send(config, str(kind), "same words") is True
        assert outcome.error is None
    pushed = [json.loads(line)["title"] for line in (tmp_path / "push-outbox.jsonl").read_text().splitlines()]
    kept = [(event["status"], event["channel"]) for event in _journal(tmp_path)]
    if policy == "decisions":
        assert pushed == ["question", "merge", "order"]
        assert kept == [("not pushed", kind) for kind in (*notify.STATUS_KINDS, "untyped")]
    else:
        assert pushed == [*notify.KINDS, "None"] and kept == []


def test_kind_does_not_leak_out_of_its_block(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    config = Config(repo="owner/repo", notify_filter="decisions")
    with notify.sending("question"):
        assert notify.send(config, "inside", "line")
    assert notify.send(config, "after", "line")
    pushed = [json.loads(line)["title"] for line in (tmp_path / "push-outbox.jsonl").read_text().splitlines()]
    assert pushed == ["inside"]


def test_failed_delivery_is_reported_and_kept_as_a_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    (tmp_path / "push-outbox.jsonl").mkdir()
    with notify.sending("merge") as outcome:
        assert notify.send(Config(repo="owner/repo"), "Merge", "PR #3") is False
    assert "IsADirectoryError" in outcome.error
    [event] = _journal(tmp_path)
    assert event["status"] == "failed" and event["channel"] == "merge" and "PR #3" in event["summary"]


def test_without_repo_or_origin_nothing_is_written_and_the_reason_is_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert notify.checkout_repo() is None
    assert "repository" in notify.deliver(Config(notify="claude-push"), "Title", "Line")
    assert "repository" in notify.deliver(Config(notify_filter="decisions"), "Title", "Line", kind="status")


def test_notify_none_pushes_nothing_but_keeps_status_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    config = Config(repo="owner/repo", notify="none")
    for kind in ("question", "silence"):
        with notify.sending(kind):
            assert notify.send(config, kind, "line") is True
    assert not (tmp_path / "push-outbox.jsonl").exists()
    assert [event["channel"] for event in _journal(tmp_path)] == ["silence"]


def test_producer_root_names_the_repository_instead_of_the_working_folder(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    project = tmp_path / "project"
    git("init", "-q", str(project))
    # The origin lives only in an included file; Git, not a file parser, must resolve it.
    included = tmp_path / "origin.inc"
    included.write_text('[remote "origin"]\n\turl = git@github.com:owner/project.git\n')
    git("-C", str(project), "config", "include.path", str(included))
    # A linked worktree, laid out as `git worktree add` does (tests may not commit).
    gitdir = project / ".git/worktrees/linked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {gitdir}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    stores = []
    monkeypatch.setattr(StateStore, "for_repo", lambda repo: stores.append(repo) or StateStore(tmp_path / "state"))
    for root in (project / "sub", linked / "deep" / "er"):
        with notify.sending("silence", root) as outcome:
            assert notify.send(Config(), "Silent", "line") is True
        assert outcome.error is None
    assert stores == ["owner/project", "owner/project"]
    assert notify.send(Config(), "Silent", "line") is False, "The working folder has no origin"


@pytest.mark.parametrize("config", [Config(repo="owner/repo", notify="none"),
                                    Config(repo="owner/repo", notify_filter="decisions")])
def test_an_unpushed_fact_whose_notice_cannot_be_written_is_not_acknowledged(tmp_path, monkeypatch, config):
    from foragent import orch_inbox

    def broken(*args, **kwargs):
        raise OSError("journal unwritable")

    monkeypatch.setattr(StateStore, "for_repo", lambda repo: StateStore(tmp_path))
    monkeypatch.setattr(orch_inbox, "record_notice", broken)
    for kind in ("silence", "blocked", "orchestrator-stopped", None):
        with notify.sending(kind) as outcome:
            assert notify.send(config, "Silent", "line") is False, "A caller would mark it delivered"
        assert "journal unwritable" in outcome.error
    with notify.sending("question"):
        assert notify.send(config, "Question", "line") is True, "Decisions live in their own record"
