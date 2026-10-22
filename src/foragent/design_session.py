"""Design mode belongs to planning, outside gate runs and worker lifetimes."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from hashlib import sha256
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from foragent import design_board, watch_open
from foragent.design import DesignError
from foragent.state import StateStore


def topic_directory(store: StateStore, topic: str) -> Path:
    if not topic.strip():
        raise DesignError("topic must not be empty")
    # A topic is a label, not a filesystem path (it can include Korean or /).
    return store.root / "design" / sha256(topic.encode("utf-8")).hexdigest()[:24]


def _session(store: StateStore) -> dict:
    try:
        return json.loads((store.root / "design-preview.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"panes": [], "servers": []}


def _save(store: StateStore, session: dict) -> None:
    store._write_json(store.root / "design-preview.json", session)


def _call(argv: list[str], root: Path) -> str:
    return subprocess.run(argv, cwd=root, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, check=True, timeout=10).stdout


def _handle(value) -> str | None:
    if isinstance(value, dict):
        for key in ("handle", "terminal", "result", "data", "id"):
            item = value.get(key)
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict) and (found := _handle(item)):
                return found
    return None


def _terminal(store: StateStore, root: Path) -> tuple[str, str, str]:
    # The saved lane view is the right-hand destination, not the caller pane.
    if terminal := watch_open._recorded_terminal(store, root):
        return "orca", *terminal
    handle = os.environ.get("ORCA_TERMINAL_HANDLE")
    if handle:
        name = (os.environ.get("ORCA_CLI_COMMAND") or
                ("orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else "orca"))
        if executable := shutil.which(name):
            return "orca", executable, handle
    if os.environ.get("TMUX") and (executable := shutil.which("tmux")):
        return "tmux", executable, os.environ.get("TMUX_PANE", "")
    raise DesignError("terminal previews need an existing Orca pane or tmux session; use --url for a web preview")


def _split(terminal: tuple[str, str, str], command: str, root: Path) -> dict:
    backend, executable, target = terminal
    if backend == "orca":
        output = _call([executable, "terminal", "split", "--terminal", target,
                        "--direction", "vertical", "--command", command, "--json"], root)
        handle = _handle(json.loads(output))
    else:
        output = _call([executable, "split-window", "-h", "-P", "-F", "#{pane_id}",
                        *(["-t", target] if target else []), "-c", str(root), command], root)
        handle = output.strip()
    if not handle or handle == target:
        raise DesignError("terminal split did not return a new preview pane handle")
    return {"backend": backend, "executable": executable, "handle": handle}


def _done(store: StateStore, root: Path) -> None:
    session = _session(store)
    for pane in list(session["panes"]):
        executable, handle = pane["executable"], pane["handle"]
        argv = ([executable, "terminal", "close", "--terminal", handle, "--json"]
                if pane["backend"] == "orca" else [executable, "kill-pane", "-t", handle])
        try:
            _call(argv, root)
        except subprocess.CalledProcessError:
            # Manually closed previews are already done; live panes remain retryable.
            probe = ([executable, "terminal", "show", "--terminal", handle, "--json"]
                     if pane["backend"] == "orca" else [executable, "display-message", "-p", "-t", handle, "#{pane_id}"])
            try:
                _call(probe, root)
            except subprocess.CalledProcessError:
                pass
            else:
                raise DesignError(f"could not close preview pane {handle}; run fa design done again")
        session["panes"].remove(pane)
        _save(store, session)
    for server in list(session["servers"]):
        try:
            with urlopen(Request(server["stop_url"], data=b"", method="POST"), timeout=3):
                pass
        except URLError as exc:
            # Connection refusal means the saved server has already exited.
            if not isinstance(exc.reason, ConnectionRefusedError):
                raise DesignError(f"could not stop design server: {exc}") from exc
        session["servers"].remove(server)
        _save(store, session)
    (store.root / "design-preview.json").unlink(missing_ok=True)


def done(store: StateStore, root: Path) -> None:
    with StateStore(store.root / "design")._state_lock():
        _done(store, root)


def _start_server(store: StateStore, directory: Path, root: Path) -> dict:
    log = directory / "server.log"
    with log.open("ab") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "foragent.cli", "design", "serve", "--repo-root", str(root),
             "--directory", str(directory)], cwd=root, stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
        )
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            try:
                return json.loads((directory / "ready.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        raise DesignError(f"local design server did not start; see {log}")
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        raise


def preview(args, store: StateStore) -> None:
    root = args.repo_root.resolve()
    topic = topic_directory(store, args.topic)
    commands, urls = args.preview_commands, args.url
    references = None
    terminal = None
    if args.stage == "board":
        if args.references is None or commands or urls:
            raise DesignError("--stage board needs --references and no draft commands/URLs")
        references = design_board.read_references(args.references.resolve())
    else:
        if args.references is not None or not (commands or urls):
            raise DesignError("preview needs --stage board --references FILE, or --command/--url")
        if args.stage == "drafts" and not 2 <= len(commands or urls) <= 3:
            raise DesignError("--stage drafts compares 2–3 running drafts; repeat --command or --url")
        if commands:
            for command in commands:
                try:
                    if not shlex.split(command):
                        raise ValueError("empty command")
                except ValueError as exc:
                    raise DesignError(f"invalid preview command: {exc}") from exc
            terminal = _terminal(store, root)
        for url in urls:
            design_board.web_url(url)

    directory = None
    if references is not None or len(urls) > 1:
        topic.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="preview-", dir=topic))
        if references is not None:
            design_board.prepare_board(store, directory, args.topic, references, root, topic / "picks.json")
        else:
            design_board.prepare_drafts(store, directory, args.topic, urls)

    with StateStore(store.root / "design")._state_lock():
        _done(store, root)
        session = {"topic": args.topic, "panes": [], "servers": []}
        _save(store, session)
        try:
            if terminal:
                for command in commands:
                    pane = _split(terminal, command, root)
                    session["panes"].append(pane)
                    _save(store, session)
                    # Split successive previews so they stay together beside the lane view.
                    terminal = pane["backend"], pane["executable"], pane["handle"]
                print(f"Design preview: {len(commands)} running terminal draft(s).")
            else:
                url = urls[0] if directory is None else None
                if directory is not None:
                    server = _start_server(store, directory, root)
                    session["servers"].append(server)
                    _save(store, session)
                    url = server["url"]
                if not webbrowser.open(url):
                    raise DesignError(f"browser could not open {url}")
                print(f"Design preview: {url}")
                if references is not None:
                    print(f"Saved picks: {topic / 'picks.json'}")
        except BaseException:
            _done(store, root)
            raise


def run(args, store: StateStore) -> int:
    if args.action == "preview":
        preview(args, store)
    elif args.action == "picks":
        picks = design_board.read_picks(topic_directory(store, args.topic) / "picks.json")
        if args.json:
            print(json.dumps(picks, ensure_ascii=False))
        else:
            print("👍 Likes: " + (", ".join(picks["likes"]) or "(none)"))
            print("👎 Dislikes: " + (", ".join(picks["dislikes"]) or "(none)"))
    elif args.action == "done":
        done(store, args.repo_root.resolve())
        print("Design preview closed; the lane view is available again. Saved picks are kept.")
    elif args.action == "serve":
        design_board.serve(store, args.directory)
    return 0
