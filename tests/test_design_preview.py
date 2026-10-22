"""Preview ownership, failure recovery and the local board's HTTP boundary."""

import json
import subprocess
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from foragent import cli, design_board, design_session
from foragent.config import Config
from foragent.design import DesignError
from foragent.state import StateStore


@pytest.fixture
def local_cli(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda root: Config(repo="test/design"))
    monkeypatch.setattr(StateStore, "for_repo", classmethod(lambda cls, repo: store))
    monkeypatch.setattr(cli, "GhTracker", lambda *a: pytest.fail("design must stay local"))
    return store, lambda *args: cli.main(["design", *args, "--repo-root", str(tmp_path)])


@pytest.mark.parametrize("options", [
    ["--stage", "board"],
    ["--stage", "drafts", "--url", "http://localhost:3000"],
    ["--command", "   "],
    ["--command", "'unfinished"],
    ["--url", "javascript:alert(1)"],
    ["--url", "http://[invalid"],
])
def test_invalid_preview_preserves_existing_session(local_cli, tmp_path, capsys, options):
    store, run = local_cli
    old = {"topic": "old", "panes": [{"handle": "keep-this"}], "servers": []}
    store._write_json(store.root / "design-preview.json", old)
    assert run("preview", *options) == 1
    assert "foragent:" in capsys.readouterr().err
    assert json.loads((store.root / "design-preview.json").read_text()) == old


def test_failed_terminal_comparison_closes_only_created_previews(local_cli, tmp_path, monkeypatch):
    store, run = local_cli
    monkeypatch.setattr(design_session, "_terminal", lambda *a: ("tmux", "/tools/tmux", "%planning"))
    calls = []

    def call(argv, root):
        calls.append(argv)
        if "split-window" in argv:
            if len([a for a in calls if "split-window" in a]) == 2:
                raise subprocess.CalledProcessError(1, argv)
            return "%preview-a\n"
        return ""

    monkeypatch.setattr(design_session, "_call", call)
    assert run("preview", "--stage", "drafts", "--command", "python 'draft a.py'",
               "--command", "python 'draft b.py'") == 1
    assert [call for call in calls if "kill-pane" in call] == [["/tools/tmux", "kill-pane", "-t", "%preview-a"]]
    assert not (store.root / "design-preview.json").exists()
    assert run("done") == 0
    assert len([call for call in calls if "kill-pane" in call]) == 1


def test_tmux_preview_replacement_closes_old_pane_and_keeps_caller(local_cli, tmp_path, monkeypatch):
    store, run = local_cli
    monkeypatch.setattr(design_session, "_terminal", lambda *a: ("tmux", "/tools/tmux", "%planning"))
    handles = iter(("%first", "%second"))
    calls = []

    def call(argv, root):
        calls.append(argv)
        return next(handles) if "split-window" in argv else ""

    monkeypatch.setattr(design_session, "_call", call)
    assert run("preview", "--command", "python 'draft a.py'") == 0
    assert run("preview", "--command", "python 'draft b.py'") == 0
    assert calls[0][-1] == "python 'draft a.py'"
    assert calls[1] == ["/tools/tmux", "kill-pane", "-t", "%first"]
    assert run("done") == run("done") == 0
    assert [call[-1] for call in calls if "kill-pane" in call] == ["%first", "%second"]
    assert store.active_runs() == []


def test_missing_picks_are_read_only_even_for_a_path_like_topic(local_cli, tmp_path, capsys):
    store, run = local_cli
    assert run("picks", "--topic", "../../다른 화면", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"likes": [], "dislikes": []}
    assert not store.root.exists()


def test_manifest_validation_and_relative_images(tmp_path):
    image = tmp_path / "reference.gif"
    image.write_bytes(b"GIF89a")
    manifest = tmp_path / "references.json"
    ref = {"id": "one", "direction": "Calm", "title": "<A>", "url": "https://example.com", "kind": "image",
           "image": image.name}
    manifest.write_text(json.dumps({"references": [ref]}))
    assert design_board.read_references(manifest)[0]["image"] == str(image)
    manifest.write_text(json.dumps({"references": [ref, ref]}))
    with pytest.raises(DesignError, match="duplicate reference id"):
        design_board.read_references(manifest)
    ref["image"] = "missing.png"
    manifest.write_text(json.dumps({"references": [ref]}))
    with pytest.raises(DesignError, match="local PNG"):
        design_board.read_references(manifest)


def test_board_http_rejects_unrelated_files_and_votes_then_restores_picks(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    topic = design_session.topic_directory(store, "screen")
    directory = topic / "first"
    source = tmp_path / "reference.png"
    source.write_bytes(b"local image")
    references = [{"id": "one", "direction": "Calm", "title": '<script>"title"</script>',
                   "url": "https://example.com", "kind": "image", "image": str(source)}]
    design_board.prepare_board(store, directory, "screen", references, tmp_path, topic / "picks.json")
    servers = []

    def init(server, address, handler):
        server.server_port = 8765
        server.RequestHandlerClass = handler
        servers.append(server)

    monkeypatch.setattr(design_board.HTTPServer, "__init__", init)
    for name in ("serve_forever", "server_close", "shutdown"):
        monkeypatch.setattr(design_board.HTTPServer, name, lambda *a: None)
    design_board.serve(store, directory)

    def request(path, *, fields=None, host="127.0.0.1:8765"):
        method = "POST" if fields is not None else "GET"
        content = urlencode(fields or {}).encode()
        raw = (f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(content)}\r\n\r\n").encode() + content

        class Connection:
            output = BytesIO()

            def makefile(self, *a):
                return BytesIO(raw)

            def sendall(self, data):
                self.output.write(data)

        connection = Connection()
        servers[0].RequestHandlerClass(connection, ("127.0.0.1", 1111), servers[0])
        head, _, body = connection.output.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), body

    token = json.loads((directory / "board.json").read_text())["token"]
    for path in ("/board.json", "/ready.json", "/server.log", "/../picks.json", "/images/../../picks.json"):
        assert request(path)[0] == 404
    assert request("/", host="foreign.invalid")[0] == 403
    assert request("/", host="[")[0] == 403
    for fields in ({"id": "unknown", "vote": "likes"}, {"id": "one", "vote": "unknown"}, {"id": "one"}):
        assert request(f"/{token}/picks", fields=fields)[0] == 400
    assert not (topic / "picks.json").exists()
    assert request(f"/{token}/picks", fields={"id": "one", "vote": "likes"})[0] == 303
    assert design_board.read_picks(topic / "picks.json") == {"likes": ["one"], "dislikes": []}
    assert b'aria-pressed="true"' in request("/")[1]
    assert b"<script>" not in request("/")[1]
    second = topic / "second"
    design_board.prepare_board(store, second, "screen", references, tmp_path, topic / "picks.json")
    assert 'aria-pressed="true"' in (second / "index.html").read_text()
    assert source.read_bytes() == b"local image"


def test_server_failure_stops_child_without_a_gate_record(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state")
    directory = store.root / "design" / "preview"
    directory.mkdir(parents=True)
    stopped = []
    monkeypatch.setattr(design_session.subprocess, "Popen", lambda *a, **kw: SimpleNamespace(
        poll=lambda: 1, terminate=lambda: stopped.append("terminated"), wait=lambda **kw: None,
    ))
    with pytest.raises(DesignError, match="did not start"):
        design_session._start_server(store, directory, tmp_path)
    assert stopped == ["terminated"]
    assert store.active_runs() == []
