"""Acceptance test for view-notify. Locked by Foragent; do not edit."""

import pytest


def notify_store(tmp_path, monkeypatch):
    from foragent.state import StateStore

    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo, **kw: store))
    return store


@pytest.mark.parametrize("target", [None, "claude-push"])
def test_default_and_cli_append_push_outbox_and_none_is_silent(git_history, tmp_path, monkeypatch, target):
    import json
    import subprocess
    from types import SimpleNamespace

    from foragent.config import Config
    from foragent.notify import send
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch)
    main = bind_cli(case, monkeypatch)
    processes = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        processes.append((a, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    send(Config(repo="owner/repo", notify=target), "Title", "Line", "https://example.test/issue/1")
    assert main(["notify", "--title", "CLI title", "--line", "CLI line"]) == 0
    path = case.store.root / "push-outbox.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(row["title"], row["line"]) for row in records] == [("Title", "Line"), ("CLI title", "CLI line")]
    assert records[0]["link"] == "https://example.test/issue/1"
    before = path.read_bytes()
    send(Config(repo="owner/repo", notify="none"), "Ignored", "Ignored", None)
    assert path.read_bytes() == before
    assert processes == []


@pytest.mark.parametrize("target,url", [("slack:https://hooks.example.test/abc", "https://hooks.example.test/abc"),
                                         ("ntfy:foragent-tests", "https://ntfy.sh/foragent-tests")])
def test_http_targets_post_payload_through_injected_transport(tmp_path, monkeypatch, target, url):
    from contextlib import nullcontext
    import json
    from types import SimpleNamespace
    import urllib.request

    calls = []

    def transport(request, *args, **kwargs):
        calls.append(request)
        return nullcontext(SimpleNamespace(status=200, read=lambda: b"ok"))

    monkeypatch.setattr(urllib.request, "urlopen", transport)
    from foragent.config import Config
    from foragent import notify

    if hasattr(notify, "urlopen"):
        monkeypatch.setattr(notify, "urlopen", transport)
    notify_store(tmp_path, monkeypatch)
    notify.send(Config(repo="owner/repo", notify=target), "Build", "Ready", "https://example.test/pr/1")
    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == url and request.get_method() == "POST"
    content = request.data.decode()
    if target.startswith("slack:"):
        payload = json.loads(content)
        assert set(payload) == {"text"}
        content = payload["text"]
    assert all(value in content for value in ("Build", "Ready", "https://example.test/pr/1"))


@pytest.mark.parametrize("target", ["command:/offline/notifier", "macos"])
def test_process_targets_use_injected_runner_and_command_environment(tmp_path, monkeypatch, target):
    import subprocess
    from types import SimpleNamespace

    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", runner)
    from foragent.config import Config
    from foragent import notify

    if hasattr(notify, "run"):
        monkeypatch.setattr(notify, "run", runner)
    notify_store(tmp_path, monkeypatch)
    notify.send(Config(repo="owner/repo", notify=target), 'A "title"', "Ready", "https://example.test")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    if target.startswith("command:"):
        assert argv[0] == "/offline/notifier"
        assert {key: kwargs["env"][key] for key in ("FA_TITLE", "FA_LINE", "FA_LINK")} == {
            "FA_TITLE": 'A "title"', "FA_LINE": "Ready", "FA_LINK": "https://example.test"}
    else:
        assert argv[0] == "osascript"


def test_delivery_failure_is_logged_without_escaping_and_inspect_uses_default_outbox(
    git_history, tmp_path, monkeypatch, caplog, capsys,
):
    import subprocess
    from types import SimpleNamespace
    import urllib.request

    from foragent.config import Config
    from foragent import notify
    from test_orch_review_handoff import bind_cli, make_case

    def broken(*args, **kwargs):
        raise OSError("offline notifier failed")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", broken)
        patch.setattr(urllib.request, "urlopen", broken)
        if hasattr(notify, "run"):
            patch.setattr(notify, "run", broken)
        if hasattr(notify, "urlopen"):
            patch.setattr(notify, "urlopen", broken)
        notify_store(tmp_path, patch)
        for target in ("command:/offline/notifier", "slack:https://hooks.example.test/failing"):
            notify.send(Config(repo="owner/repo", notify=target), "Title", "Line", None)
    assert caplog.records or capsys.readouterr().err
    case = make_case(git_history, tmp_path, monkeypatch)
    case.tracker.add_labels(1, ["needs-human"])
    case.tracker.comment(1, "<!-- foragent:needs-human reason=escalated -->\nChoose locale")
    main = bind_cli(case, monkeypatch)
    processes = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        processes.append((a, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert main(["inspect", "notify"]) == 0
    assert (case.store.root / "push-outbox.jsonl").is_file()
    assert processes == []
