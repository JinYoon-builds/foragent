"""Acceptance test for design-session. Locked by Foragent; do not edit."""

from datetime import timedelta
import json
from pathlib import Path
import shlex
import subprocess

import yaml

from test_local_truth import record_gh
from test_claude_sessions import sessions_offline
from test_orch_review_handoff import bind_cli, make_case, tick_context
from test_skills_autosync import cli_exit, isolated_home


class TerminalCommands:
    """Record opener requests, with a controllable terminal lifetime."""

    def __init__(self, monkeypatch, *, tmux=False, claude_reply=None):
        import shutil
        import webbrowser

        self.calls, self.alive, self.on_create = [], True, None
        self.urls = []
        for name in ("open", "open_new", "open_new_tab"):
            monkeypatch.setattr(webbrowser, name, lambda url, *a, **kw: self.urls.append(url) or True)
        for key in ("ORCA_TERMINAL_HANDLE", "TMUX", "TERM_PROGRAM", "WEZTERM_PANE", "KITTY_WINDOW_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("TMUX" if tmux else "ORCA_TERMINAL_HANDLE", "offline-source")
        monkeypatch.setattr(shutil, "which", lambda name: f"/offline/{name}"
                            if name in {"claude", "open", "tmux" if tmux else "orca"} else None)
        original_run, original_popen = subprocess.run, subprocess.Popen

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and Path(argv[0]).name in {"orca", "tmux", "claude", "osascript", "open"}:
                self.calls.append(list(argv))
                if Path(argv[0]).name == "claude" and claude_reply is not None:
                    assert argv[1] in {"agents", "rm"}, "Claude starts only inside the fake tab"
                    return subprocess.CompletedProcess(argv, 0, stdout=claude_reply(list(argv), kwargs), stderr="")
                assert Path(argv[0]).name in {"orca", "tmux", "open"}, "Only the opener may run; Claude stays in the fake terminal"
                if argv[1:3] == ["terminal", "show"] and not self.alive:
                    raise subprocess.CalledProcessError(1, argv, stderr="terminal is gone")
                if argv[1:3] == ["terminal", "create"] and self.on_create is not None:
                    self.on_create()
                output = json.dumps({"handle": "term_design", "terminal": "term_design", "id": "term_design"})
                return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
            return original_run(argv, *args, **kwargs)

        def popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and Path(argv[0]).name in {"orca", "tmux", "claude", "osascript", "open"}:
                result = run(argv, *args, **kwargs)

                class Opened:
                    pid, returncode = 99001, 0

                    def poll(self):
                        return 0

                    def wait(self, timeout=None):
                        return 0

                    def communicate(self, *args, **kwargs):
                        return result.stdout, result.stderr

                return Opened()
            return original_popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(subprocess, "Popen", popen)


def design_case(git_history, tmp_path, monkeypatch, *, tmux=False):
    from foragent import cli
    from foragent.tracker import GhTracker

    case = make_case(git_history, tmp_path, monkeypatch, skill_libraries=[], test_command="true",
                     max_parallel=1, lanes={"view": {"paths": ["**"]}})
    case.store.finish_run(1, "fixture-idle")
    case.tracker.remove_labels(1, ["running"])
    case.tracker.add_labels(1, ["lane:view"])
    main = bind_cli(case, monkeypatch)
    # CLI observations must reach the real gh boundary, not disappear into FakeTracker.
    monkeypatch.setattr(cli, "GhTracker", GhTracker)
    commands = TerminalCommands(monkeypatch, tmux=tmux)
    if not tmux:
        # The planning pane is offline-source; its existing lane view is a different pane.
        (case.store.root / "watch-terminal.json").write_text(json.dumps({
            "executable": "/offline/orca", "handle": "term_lane_view",
        }), encoding="utf-8")
    gh = record_gh(monkeypatch)
    return case, main, commands, gh


def terminal_draft(case):
    draft = case.root / "docs/design/lane-view/preview with spaces.py"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("print('draft A | draft B | draft C')\n", encoding="utf-8")
    return shlex.join(["python3", str(draft)])


def assert_no_design_tab(commands):
    assert not any(argv[1:3] == ["terminal", "create"] or "new-window" in argv
                   for argv in commands.calls), commands.calls
    assert not any("design: " in arg for argv in commands.calls for arg in argv), commands.calls


# D53 keeps these historical function names, including the obsolete open/new-window wording.
# The preview tool is chosen by the agent: --command and --url carry that choice explicitly.
def test_design_session_explicit_orca_open_ignores_view_off_and_places_preview_beside_it(
    git_history, tmp_path, monkeypatch,
):
    from foragent.state import StateStore

    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    monkeypatch.setenv("FORAGENT_VIEW", "off")
    command = terminal_draft(case)
    assert cli_exit(main, ["design", "preview", "--topic", "lane-view", "--repo-root", str(case.root),
                           "--command", command]) == 0, "Design mode needs the preview entry point"
    assert_no_design_tab(commands)
    splits = [argv for argv in commands.calls if argv[1:3] == ["terminal", "split"]]
    assert len(splits) == 1 and splits[0][splits[0].index("--direction") + 1] == "vertical"
    assert splits[0][splits[0].index("--terminal") + 1] == "term_lane_view"
    assert shlex.split(splits[0][splits[0].index("--command") + 1]) == shlex.split(command)
    records = [path.read_text(encoding="utf-8") for path in case.store.root.rglob("*.json")]
    assert any("term_design" in record for record in records), "Persist the returned preview pane handle"

    # A fresh reader in another caller pane must close the saved preview, not the caller or lane view.
    case.store = StateStore(case.store.root, now=lambda: case.clock[0])
    monkeypatch.setenv("ORCA_TERMINAL_HANDLE", "offline-other-caller")
    assert cli_exit(main, ["design", "done", "--repo-root", str(case.root)]) == 0
    closes = [argv for argv in commands.calls if argv[1:3] == ["terminal", "close"]]
    assert len(closes) == 1 and closes[0][closes[0].index("--terminal") + 1] == "term_design"
    assert len([argv for argv in commands.calls if argv[1:3] == ["terminal", "split"]]) == 1
    assert cli_exit(main, ["design", "done", "--repo-root", str(case.root)]) == 0
    assert [argv for argv in commands.calls if argv[1:3] == ["terminal", "close"]] == closes
    assert case.store.active_runs() == []
    assert_no_design_tab(commands)
    assert gh == []


def test_design_session_tmux_open_without_topic_creates_a_new_window_and_returns(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch, tmux=True)
    draft = case.root / "docs/design/web-comparison/index.html"
    draft.parent.mkdir(parents=True)
    draft.write_text("<main>draft A | draft B | draft C</main>\n", encoding="utf-8")
    url = "http://127.0.0.1:8765/web-comparison/?draft=A%20B&compare=3"
    assert cli_exit(main, ["design", "preview", "--topic", "web-comparison", "--repo-root", str(case.root),
                           "--url", url]) == 0, "Web drafts open the agent's local preview URL"
    assert url in commands.urls or any(url in argv for argv in commands.calls)
    assert_no_design_tab(commands)
    assert not any(argv[1:3] == ["terminal", "split"] or "split-window" in argv
                   for argv in commands.calls), commands.calls
    assert not any("attach-session" in argv or "wait-for" in argv for argv in commands.calls)
    # Edge: a browser preview has no terminal pane to close, even on repeated cleanup.
    for _ in range(2):
        assert cli_exit(main, ["design", "done", "--repo-root", str(case.root)]) == 0
    assert not any(argv[1:3] == ["terminal", "close"] or "kill-pane" in argv
                   for argv in commands.calls), commands.calls
    assert_no_design_tab(commands)
    assert case.store.active_runs() == []
    assert gh == []


def test_design_session_is_recorded_outside_active_runs_and_never_stalls_or_occupies_a_lane(
    git_history, tmp_path, monkeypatch,
):
    from foragent.scheduler import tick

    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    assert cli_exit(main, ["design", "preview", "--topic", "lane-view", "--repo-root", str(case.root),
                           "--command", terminal_draft(case)]) == 0
    assert case.store.active_runs() == []
    case.clock[0] += timedelta(minutes=45)
    case.tracker.record_label(1, "approved", "human")
    launched, killed = [], []
    ctx = tick_context(case)
    ctx.launcher = lambda number: launched.append(number) or 7000 + number
    ctx.kill_runner = killed.append
    report = tick(ctx)
    assert not any("stall" in action for action in report.actions), report.actions
    assert launched == [1], "A silent preview must not consume the only worker slot"
    assert [(run.number, run.lane) for run in case.store.active_runs()] == [(1, "view")], report.actions
    assert not case.tracker.get_issue(1).labels & {"blocked", "needs-human"}
    assert killed == []
    assert commands.alive and not any(argv[1:3] == ["terminal", "close"] or "rm" in argv
                                      for argv in commands.calls)
    assert_no_design_tab(commands)
    assert gh == []


def test_design_session_skill_defines_comparison_tools_durable_drafts_and_handoff():
    path = Path(__file__).resolve().parents[2] / "skills/fa-design-session/SKILL.md"
    assert path.is_file(), "D72 addendum 3 needs the planning session's design-mode guide"
    source = path.read_text(encoding="utf-8")
    assert source.startswith("---\n")
    assert yaml.safe_load(source.split("---", 2)[1])["name"] == "fa-design-session"
    text = " ".join(source.lower().split())
    groups = {
        "planning session": ("planning session", "기획 세션"),
        "design mode": ("design mode", "design-mode", "디자인 모드"),
        "same window": ("same window", "same pane as the planning", "같은 창"),
        "multiple drafts": ("2–3", "2-3", "2~3", "two or three", "2~3개"),
        "comparison": ("side by side", "side-by-side", "나란히"),
        "terminal preview": ("terminal", "터미널"),
        "Orca panes": ("orca",),
        "web preview": ("local server", "로컬 서버"),
        "browser": ("browser", "브라우저"),
        "gstack screenshots": ("gstack",),
        "Playwright": ("playwright",),
        "comparison page": ("comparison page", "비교 페이지"),
        "design-file tool": ("figma",),
        "existing design files": ("design files", "디자인 파일"),
        "mobile preview": ("simulator", "시뮬레이터"),
        "screenshots": ("screenshot", "스크린샷"),
        "find a missing tool": ("find a tool", "find tools", "propose a tool", "찾아", "도구를 찾"),
        "record decisions": ("decision", "결정"),
        "chosen draft": ("chosen draft", "selected draft", "선택한 시안", "고른 시안"),
        "handoff": ("orchestrator", "오케스트레이터"),
        "durable request": ("fa orch request",),
        "lane issue split": ("lane issues", "issues by lane", "레인 이슈"),
        "visual done-when": ("same screen as the draft", "시안과 같은 화면"),
        "implementing worker": ("implementing worker", "implementation worker", "구현하는 작업자", "구현 작업자"),
        "normally no worker conversation": ("normally never talks", "normally does not talk", "normally do not talk",
                                             "usually does not talk", "usually do not talk", "no need to talk",
                                             "필요는 보통 없다", "대화할 필요가 없", "이야기할 필요는 보통 없"),
    }
    missing = [meaning for meaning, alternatives in groups.items() if not any(word in text for word in alternatives)]
    assert not missing, f"Missing design-session guidance: {missing}"
    assert "docs/design/<topic>/" in source and "docs/design/<topic>/HANDOFF.md" in source
    assert any(word in text for word in ("never one", "never show only one", "do not show only one", "하나만")), (
        'Explicitly reject showing one draft and asking "how is it?"'
    )


def test_design_session_orchestrator_suggests_design_and_turns_handoff_into_lane_issues():
    path = Path(__file__).resolve().parents[2] / "skills/fa-orchestrator/SKILL.md"
    text = " ".join(path.read_text(encoding="utf-8").lower().split())
    assert "handoff.md" in text and "request" in text, "Route design HANDOFF/request into lane issues"
    assert any(word in text for word in ("planning session", "기획 세션")), "Design belongs to the planning session"
    assert any(word in text for word in ("suggest", "offer", "propose", "제안"))
    assert "lane" in text and "issue" in text and "done when" in text
    assert any(word in text for word in ("same screen as the draft", "시안과 같은 화면"))


def design_page(path):
    """Read semantic HTML without a browser, DOM library or network."""
    from html.parser import HTMLParser
    from xml.etree.ElementTree import Element, SubElement

    class Page(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = Element("document")
            self.stack = [self.root]

        def handle_starttag(self, tag, attrs):
            node = SubElement(self.stack[-1], tag, dict(attrs))
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                           "link", "meta", "param", "source", "track", "wbr"}:
                self.stack.append(node)

        def handle_endtag(self, tag):
            for index in range(len(self.stack) - 1, 0, -1):
                if self.stack[index].tag == tag:
                    del self.stack[index:]
                    break

        def handle_data(self, data):
            node = self.stack[-1]
            if len(node):
                node[-1].tail = (node[-1].tail or "") + data
            else:
                node.text = (node.text or "") + data

    page = Page()
    page.feed(path.read_text(encoding="utf-8"))
    return page.root


def design_text(element):
    return " ".join("".join(element.itertext()).split())


def design_browser_urls(commands):
    return commands.urls + [str(arg) for argv in commands.calls for arg in argv[1:]
                            if str(arg).startswith(("http://", "https://", "file://"))]


class LocalDesignHTTP:
    """Exercise real HTTP handlers and persistence through memory-only sockets.

    A background fa design child is replayed through the real CLI in-process.
    Only process/socket transport and serve_forever are replaced; request handlers
    and their state writes remain product code. Browser buttons use POST forms.
    """

    def __init__(self, main, monkeypatch):
        from http.server import HTTPServer
        from types import SimpleNamespace

        self.servers = []

        def server_init(server, address, handler, *args, **kwargs):
            server.server_address = (address[0], address[1] or 8765)
            server.server_name, server.server_port = "localhost", server.server_address[1]
            server.RequestHandlerClass = handler
            self.servers.append(server)

        monkeypatch.setattr(HTTPServer, "__init__", server_init)
        for method in ("serve_forever", "shutdown", "server_close"):
            monkeypatch.setattr(HTTPServer, method, lambda *a, **kw: None)
        original_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and "design" in argv and (
                Path(argv[0]).name in {"fa", "foragent"} or Path(argv[0]).name.startswith("python")
            ):
                assert cli_exit(main, list(argv[argv.index("design"):])) == 0
                return SimpleNamespace(pid=99002, poll=lambda: None, wait=lambda **kw: 0,
                                       terminate=lambda: None, kill=lambda: None)
            return original_popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", popen)

    def request(self, url, *, method="GET", fields=()):
        from io import BytesIO
        from urllib.parse import urlencode, urlsplit

        target = urlsplit(url)
        assert target.hostname in {"127.0.0.1", "localhost", "::1"}, url
        servers = [server for server in self.servers if server.server_port == target.port]
        assert len(servers) == 1, "Capture the board's real local HTTP request handler"
        body = urlencode(fields).encode("utf-8")
        path = target.path or "/"
        if target.query:
            path += "?" + target.query
        headers = (f"{method} {path} HTTP/1.1\r\nHost: {target.netloc}\r\n"
                   "Connection: close\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode("ascii")

        class Connection:
            def __init__(self):
                self.output = bytearray()

            def makefile(self, *args, **kwargs):
                return BytesIO(headers + body)

            def sendall(self, data):
                self.output.extend(data)

            def settimeout(self, timeout):
                pass

        connection = Connection()
        servers[0].RequestHandlerClass(connection, ("127.0.0.1", 12345), servers[0])
        head, separator, content = bytes(connection.output).partition(b"\r\n\r\n")
        assert separator, "The real handler must return an HTTP response"
        status = int(head.splitlines()[0].split()[1])
        assert 200 <= status < 400, (status, content)
        return content


def design_screenshots(case, monkeypatch):
    """Supply image bytes at the headless screenshot command boundary only."""
    import base64
    import shutil

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jS1sAAAAASUVORK5CYII="
    )
    captures = []
    original_run, original_which = subprocess.run, shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: f"/offline/{name}"
                        if name in {"playwright", "gstack", "npx"} else original_which(name))

    def run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and "screenshot" in argv:
            assert Path(argv[0]).name in {"playwright", "gstack", "npx"}, argv
            urls = [arg for arg in argv if str(arg).startswith("https://references.invalid/")]
            outputs = [Path(str(arg).removeprefix("--output=")) for arg in argv
                       if str(arg).endswith(".png")]
            assert len(urls) == len(outputs) == 1, argv
            output = outputs[0]
            if not output.is_absolute():
                output = Path(kwargs.get("cwd", case.root)) / output
            assert output.resolve().is_relative_to(case.store.root.resolve()), output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(png)
            captures.append((urls[0], output))
            return subprocess.CompletedProcess(argv, 0, stdout=str(output), stderr="")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return png, captures


def design_picks(main, case, capsys, topic):
    capsys.readouterr()
    assert cli_exit(main, ["design", "picks", "--topic", topic, "--repo-root", str(case.root), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    return {key: sorted(result[key]) for key in ("likes", "dislikes")}


# D73 addendum 3: references/directions are supplied by the planning agent.
# --references takes {"references": [{id, direction, title, url, kind, image?}]}.
# Drafts repeat --command (terminal) or --url (web); picks contain reference IDs.
def test_design_session_board_groups_local_thumbnails_and_saves_clicked_picks(
    git_history, tmp_path, monkeypatch, capsys,
):
    from urllib.parse import urljoin, urlsplit

    from conftest import git_command
    from foragent.state import StateStore

    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    http = LocalDesignHTTP(main, monkeypatch)
    png, captures = design_screenshots(case, monkeypatch)
    terminal_image = tmp_path / "terminal-reference.png"
    terminal_image.write_bytes(png)
    references = [
        {"id": "soft-site", "direction": "Soft & playful", "title": "Soft <cards>",
         "kind": "web", "url": "https://references.invalid/soft"},
        {"id": "dense-site", "direction": "Dense & calm", "title": "Dense dashboard",
         "kind": "web", "url": "https://references.invalid/dense"},
        {"id": "soft-terminal", "direction": "Soft & playful", "title": "Terminal screenshot",
         "kind": "image", "url": "https://references.invalid/terminal", "image": str(terminal_image)},
    ]
    manifest = tmp_path / "references.json"
    manifest.write_text(json.dumps({"references": references}), encoding="utf-8")
    before = git_command(case.root, "status", "--porcelain", "--untracked-files=all")
    assert cli_exit(main, ["design", "preview", "--stage", "board", "--topic", "lane-view",
                           "--repo-root", str(case.root), "--references", str(manifest)]) == 0, (
        "D73 addendum 3 needs a local reference board, not a draft or Figma tab"
    )
    boards = list(case.store.root.rglob("*.html"))
    assert len(boards) == 1 and not boards[0].resolve().is_relative_to(case.root.resolve())
    page = design_page(boards[0])
    urls = design_browser_urls(commands)
    assert len(urls) == 1 and urlsplit(urls[0]).hostname in {"127.0.0.1", "localhost", "::1"}
    board_url = urls[0]
    assert http.request(board_url) == boards[0].read_bytes(), "Serve the actual static board file"
    styles = "".join("".join(node.itertext()) for node in page.iter("style"))
    assert any(layout in "".join(styles.split()) for layout in ("display:grid", "display:flex"))
    assert sorted(url for url, _ in captures) == sorted(ref["url"] for ref in references if ref["kind"] == "web")
    assert len(list(page.iter("img"))) == len(references)
    for ref in references:
        sections = [section for section in page.iter("section") if any(
            design_text(node) == ref["direction"] for node in section.iter()
            if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
        )]
        assert len(sections) == 1, f"Keep {ref['direction']} in its own direction group"
        grouped_urls = {node.get("href") for node in sections[0].iter("a")
                        if node.get("href") in {item["url"] for item in references}}
        assert grouped_urls == {item["url"] for item in references if item["direction"] == ref["direction"]}
        links = [node for node in sections[0].iter("a") if node.get("href") == ref["url"]]
        assert len(links) == 1 and ref["title"] in design_text(sections[0])
        images = list(links[0].iter("img"))
        assert len(images) == 1, "Each local thumbnail links to the original reference"
        image_url = urljoin(board_url, images[0].get("src", ""))
        assert urlsplit(image_url).netloc == urlsplit(board_url).netloc
        assert http.request(image_url) == png

    assert design_picks(main, case, capsys, "lane-view") == {"likes": [], "dislikes": []}
    selections = [
        ("soft-site", "👍", {"likes": ["soft-site"], "dislikes": []}),
        ("dense-site", "👎", {"likes": ["soft-site"], "dislikes": ["dense-site"]}),
        ("soft-site", "👎", {"likes": [], "dislikes": ["dense-site", "soft-site"]}),
    ]
    for reference_id, emoji, expected in selections:
        forms = [form for form in page.iter("form") if any(
            node.get("value") == reference_id for node in form.iter("input")
        )]
        assert len(forms) == 1, "Each reference needs accessible thumbs-up/down form controls"
        form = forms[0]
        assert form.get("method", "get").lower() == "post"
        buttons = list(form.iter("button"))
        assert all(any(label in design_text(button) for button in buttons) for label in ("👍", "👎"))
        selected = [button for button in buttons if emoji in design_text(button)]
        assert len(selected) == 1 and selected[0].get("name") and selected[0].get("value")
        fields = [(node.get("name"), node.get("value", "")) for node in form.iter("input") if node.get("name")]
        fields.append((selected[0].get("name"), selected[0].get("value")))
        # Submit exactly the fields/action a browser sends when this button is clicked.
        http.request(urljoin(board_url, form.get("action", "")), method="POST", fields=fields)
        # Check both vote types on disk after each click, including a change of mind.
        records = [json.loads(path.read_text(encoding="utf-8")) for path in case.store.root.rglob("*.json")]
        assert any(isinstance(row, dict) and all(sorted(row.get(key, [])) == value
                                               for key, value in expected.items()) for row in records)
        case.store = StateStore(case.store.root, now=lambda: case.clock[0])
        assert design_picks(main, case, capsys, "lane-view") == expected
    assert design_picks(main, case, capsys, "another-topic") == {"likes": [], "dislikes": []}
    assert git_command(case.root, "status", "--porcelain", "--untracked-files=all") == before
    assert terminal_image.read_bytes() == png
    assert_no_design_tab(commands)
    assert gh == []


def test_design_session_picks_missing_topic_is_empty_and_does_not_open_a_preview(
    git_history, tmp_path, monkeypatch, capsys,
):
    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    assert design_picks(main, case, capsys, "not-yet-researched") == {"likes": [], "dislikes": []}
    assert commands.calls == [] and commands.urls == []
    assert gh == []


def test_design_session_drafts_stage_opens_three_running_terminal_drafts_side_by_side(
    git_history, tmp_path, monkeypatch,
):
    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    drafts = case.root / "docs/design/lane-view"
    drafts.mkdir(parents=True)
    preview_commands = []
    for name in ("A", "B", "C"):
        script = drafts / f"draft {name}.py"
        script.write_text(f"print('running terminal draft {name}')\n", encoding="utf-8")
        preview_commands.append(shlex.join(["python3", str(script)]))
    argv = ["design", "preview", "--stage", "drafts", "--topic", "lane-view", "--repo-root", str(case.root)]
    for command in preview_commands:
        argv.extend(["--command", command])
    original_run = subprocess.run
    handles = iter(("term_draft_a", "term_draft_b", "term_draft_c"))

    def run(args, *positional, **kwargs):
        result = original_run(args, *positional, **kwargs)
        if isinstance(args, (list, tuple)) and args[1:3] == ["terminal", "split"]:
            handle = next(handles)
            result.stdout = json.dumps({"handle": handle, "terminal": handle, "id": handle})
        return result

    monkeypatch.setattr(subprocess, "run", run)
    assert cli_exit(main, argv) == 0, "Show each runnable draft in its actual terminal medium"
    splits = [call for call in commands.calls if call[1:3] == ["terminal", "split"]]
    assert len(splits) == 3, "Three drafts need three preview panes"
    actual = [shlex.split(call[call.index("--command") + 1]) for call in splits]
    assert sorted(actual) == sorted(shlex.split(command) for command in preview_commands)
    assert all(call[call.index("--direction") + 1] == "vertical" for call in splits)
    assert design_browser_urls(commands) == [], "Terminal drafts must execute in terminal panes"
    assert cli_exit(main, ["design", "done", "--repo-root", str(case.root)]) == 0
    closes = [call[call.index("--terminal") + 1] for call in commands.calls
              if call[1:3] == ["terminal", "close"]]
    assert sorted(closes) == ["term_draft_a", "term_draft_b", "term_draft_c"]
    assert_no_design_tab(commands)
    assert case.store.active_runs() == [] and gh == []


def test_design_session_drafts_stage_embeds_two_running_web_drafts_in_one_comparison_page(
    git_history, tmp_path, monkeypatch,
):
    from urllib.parse import unquote, urlsplit

    case, main, commands, gh = design_case(git_history, tmp_path, monkeypatch)
    http = LocalDesignHTTP(main, monkeypatch)
    urls = ["http://127.0.0.1:8766/draft-a/?interactive=1", "http://127.0.0.1:8767/draft-b/?interactive=1"]
    argv = ["design", "preview", "--stage", "drafts", "--topic", "web-comparison", "--repo-root", str(case.root)]
    for url in urls:
        argv.extend(["--url", url])
    assert cli_exit(main, argv) == 0, "Web comparisons must embed live drafts, not screenshots"
    pages = list(case.store.root.rglob("*.html"))
    assert len(pages) == 1
    page = design_page(pages[0])
    frames = list(page.iter("iframe"))
    assert sorted(frame.get("src") for frame in frames) == sorted(urls)
    assert all(frame.get("title") for frame in frames), "Name the interactive draft frames"
    styles = "".join("".join(node.itertext()) for node in page.iter("style"))
    assert any(layout in "".join(styles.split()) for layout in ("display:grid", "display:flex"))
    opened = design_browser_urls(commands)
    assert len(opened) == 1 and opened[0] not in urls
    location = urlsplit(opened[0])
    if location.scheme == "file":
        assert Path(unquote(location.path)).resolve() == pages[0].resolve()
    else:
        assert http.request(opened[0]) == pages[0].read_bytes()
    assert not any(call[1:3] == ["terminal", "split"] for call in commands.calls)
    assert_no_design_tab(commands)
    assert case.store.active_runs() == [] and gh == []


def test_design_session_skill_distinguishes_reference_board_running_drafts_and_optional_figma():
    path = Path(__file__).resolve().parents[2] / "skills/fa-design-session/SKILL.md"
    assert path.is_file(), "D73 addendum 3 needs the planning session's design-mode guide"
    source = path.read_text(encoding="utf-8").lower()
    text = " ".join(source.split())
    required = {
        "local browser reference board": ("reference board", "레퍼런스 보드", "브라우저 보드"),
        "board command": ("--stage board",),
        "planning session reads saved picks": ("fa design picks",),
        "likes": ("likes", "👍", "좋아요"),
        "dislikes": ("dislikes", "👎", "싫어요"),
        "file persistence": ("json file", "saved to a file", "파일에 저장", "json 파일"),
        "running code": ("running code", "runnable code", "돌아가는 코드", "실행되는 코드"),
        "real medium": ("real medium", "actual medium", "진짜 매체", "실제 매체"),
        "drafts command": ("--stage drafts",),
    }
    missing = [meaning for meaning, alternatives in required.items() if not any(word in text for word in alternatives)]
    assert not missing, f"Missing D73 addendum 3 context: {missing}"
    figma = [" ".join(paragraph.split()) for paragraph in source.split("\n\n") if "figma" in paragraph]
    assert any("figma mcp" in paragraph and any(word in paragraph for word in ("optional", "option", "선택"))
               for paragraph in figma), "Figma MCP is an option; the normal preview uses running code"
