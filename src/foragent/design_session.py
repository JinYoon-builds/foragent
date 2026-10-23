"""Local reference boards and live previews owned by the planning session.

Nothing here creates a run or contacts the tracker. Reference bytes and votes live
in the repository's external state directory, alongside (not inside) lane state.
"""

from collections import defaultdict
from contextlib import suppress
from hashlib import sha256
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from foragent import watch_open
from foragent.state import StateStore


class DesignError(ValueError):
    """A preview cannot be built or opened with the supplied inputs."""


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _topic(store: StateStore, topic: str) -> Path:
    return store.root / "design" / sha256(topic.encode()).hexdigest()


def picks(store: StateStore, topic: str) -> dict:
    return _read(_topic(store, topic) / "picks.json", {"likes": [], "dislikes": []})


def _web_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("missing scheme or hostname")
        # Accessing port validates malformed and out-of-range port numbers.
        parsed.port
    except ValueError as exc:
        raise DesignError(f"Expected an http(s) URL: {url}") from exc
    return url


def _page(title: str, body: str) -> str:
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(title)}</title><style>
body {{font: 16px system-ui; margin: 2rem; background: #171922; color: #f1f3fa}}
.grid {{display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1rem}}
article {{background:#282c38; padding:1rem; border-radius:12px}}
a {{color:#c6d7ff}} img {{width:100%; height:220px; object-fit:contain}}
button {{font:inherit; padding:.6rem 1rem; margin-right:.5rem; cursor:pointer}}
button[aria-pressed="true"] {{outline:3px solid #9ecaff}}
iframe {{width:100%; height:80vh; border:0; background:white}}
</style></head><body><h1>{escape(title)}</h1>{body}</body></html>'''


def _manifest(path: Path) -> list[dict]:
    data = _read(path, None)
    refs = data.get("references") if isinstance(data, dict) else None
    if not isinstance(refs, list) or not refs:
        raise DesignError("--references needs a JSON object with a nonempty references list")
    ids = set()
    for ref in refs:
        if not isinstance(ref, dict) or any(not isinstance(ref.get(k), str) or not ref[k].strip()
                                          for k in ("id", "direction", "title", "url", "kind")):
            raise DesignError("Each reference needs id, direction, title, url and kind strings")
        if ref["id"] in ids:
            raise DesignError(f"Duplicate reference id: {ref['id']}")
        ids.add(ref["id"])
        _web_url(ref["url"])
        if ref["kind"] not in {"web", "image"}:
            raise DesignError("Reference kind must be web or image")
        if ref["kind"] == "image":
            image = ref.get("image")
            if not isinstance(image, str) or not image:
                raise DesignError("Image references need a local image path")
            source = (path.parent / image).resolve()
            if not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                raise DesignError(f"Expected a local PNG, JPEG, GIF or WebP image: {source}")
            ref["image"] = str(source)
    return refs


def _board(store: StateStore, directory: Path, topic: str, refs: list[dict], token: str) -> None:
    groups = defaultdict(list)
    media = directory / "media"
    media.mkdir(parents=True)
    saved = picks(store, topic)
    for index, ref in enumerate(refs):
        if ref["kind"] == "web":
            target = media / f"{index}.png"
            executable = shutil.which("playwright")
            if not executable:
                raise DesignError("Web references need Playwright: install it and its Chromium browser, then retry")
            subprocess.run([executable, "screenshot", "--browser", "chromium", ref["url"], str(target)],
                           check=True, capture_output=True, text=True, timeout=90)
            if not target.is_file():
                raise DesignError(f"Screenshot tool did not produce {target.name}")
        else:
            source = Path(ref["image"])
            target = media / f"{index}{source.suffix.lower()}"
            shutil.copyfile(source, target)
        identity = escape(ref["id"], quote=True)
        controls = "".join(
            f'<button name="vote" value="{vote}" aria-pressed="{str(ref["id"] in saved[vote]).lower()}">{label}</button>'
            for vote, label in (("likes", "👍"), ("dislikes", "👎")))
        groups[ref["direction"]].append(
            f'<article><h3>{escape(ref["title"])}</h3>'
            f'<a href="{escape(ref["url"], quote=True)}" target="_blank" rel="noopener noreferrer">'
            f'<img src="/{token}/media/{target.name}" alt="{escape(ref["title"], quote=True)}"></a>'
            f'<form method="post" action="/{token}/vote">'
            f'<input type="hidden" name="id" value="{identity}">{controls}</form></article>')
    body = "".join(f'<section><h2>{escape(direction)}</h2><div class="grid">{"".join(cards)}</div></section>'
                   for direction, cards in groups.items())
    (directory / "index.html").write_text(_page(topic + " — reference board", body), encoding="utf-8")
    store._write_json(directory / "references.json", {"references": refs})


def serve(store: StateStore, directory: Path, topic: str, token: str) -> None:
    """Serve only generated pages/media; the random URL also protects form writes."""
    directory = directory.resolve()
    if not directory.is_relative_to((_topic(store, topic) / "previews").resolve()):
        raise DesignError("Preview directory must be in this topic's local state")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in {f"/{token}/", f"/{token}/index.html"}:
                target, mime = directory / "index.html", "text/html; charset=utf-8"
            elif path.startswith(f"/{token}/media/"):
                name = path.removeprefix(f"/{token}/media/")
                target = directory / "media" / name
                if "/" in name or not name or target.is_symlink():
                    self.send_error(404)
                    return
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".gif": "image/gif", ".webp": "image/webp"}.get(target.suffix, "application/octet-stream")
            else:
                self.send_error(404)
                return
            try:
                content = target.read_bytes()
            except OSError:
                self.send_error(404)
                return
            # Votes remain visible across reloads without rewriting the source page.
            if target.name == "index.html":
                saved = picks(store, topic)
                content = content.decode("utf-8")
                refs = _read(directory / "references.json", {"references": []})["references"]
                for ref in refs:
                    identity = escape(ref["id"], quote=True)
                    start = content.index(f'<input type="hidden" name="id" value="{identity}">')
                    end = content.index("</form>", start)
                    fragment = content[start:end]
                    for vote in ("likes", "dislikes"):
                        fragment = re.sub(f'(value="{vote}" aria-pressed=")[^"]+',
                                          r'\g<1>' + str(ref["id"] in saved[vote]).lower(), fragment)
                    content = content[:start] + fragment + content[end:]
                content = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            path = urlsplit(self.path).path
            # A form from another site must not change local selections.
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.send_error(403)
                return
            if path == f"/{token}/stop":
                self.send_response(204)
                self.end_headers()
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path != f"/{token}/vote":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("invalid form length")
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                identity, vote = form["id"][0], form["vote"][0]
                refs = _read(directory / "references.json", {"references": []})["references"]
                if vote not in {"likes", "dislikes"} or identity not in {ref["id"] for ref in refs}:
                    raise ValueError("unknown selection")
            except (ValueError, KeyError, UnicodeError):
                self.send_error(400)
                return
            saved = picks(store, topic)
            for key in ("likes", "dislikes"):
                saved[key] = sorted((set(saved[key]) - {identity}) | ({identity} if key == vote else set()))
            store._write_json(_topic(store, topic) / "picks.json", saved)
            self.send_response(303)
            self.send_header("Location", f"/{token}/")
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    try:
        store._write_json(directory / "server.json", {
            "url": f"http://127.0.0.1:{server.server_port}/{token}/", "pid": os.getpid(),
        })
        server.serve_forever()
    finally:
        server.server_close()


def _open_page(store: StateStore, directory: Path, args, token: str) -> str:
    with (directory / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([
            sys.executable, "-m", "foragent.cli", "design", "_serve", "--repo-root", str(args.repo_root.resolve()),
            "--directory", str(directory), f"--topic={args.topic}", f"--token={token}",
        ], cwd=args.repo_root.resolve(), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready = _read(directory / "server.json", None)
        if ready:
            return ready["url"]
        if process.poll() is not None:
            raise DesignError(f"Local preview server failed; see {directory / 'server.log'}")
        time.sleep(.05)
    process.terminate()
    raise DesignError(f"Local preview server timed out; see {directory / 'server.log'}")


def _terminal_call(argv: list[str], root: Path):
    return subprocess.run(argv, cwd=root, check=True, capture_output=True, text=True, timeout=10)


def _handle(value) -> str | None:
    if isinstance(value, dict):
        for key in ("handle", "terminalHandle", "terminal", "id"):
            if isinstance(value.get(key), str) and value[key]:
                return value[key]
        for child in value.values():
            found = _handle(child)
            if found:
                return found
    return None


def done(store: StateStore, root: Path) -> None:
    path = store.root / "design" / "session.json"
    record = _read(path, {"panes": []})
    remaining = []
    errors = []
    for pane in record["panes"]:
        argv = ([pane["executable"], "terminal", "close", "--terminal", pane["handle"], "--json"]
                if pane["kind"] == "orca" else [pane["executable"], "kill-pane", "-t", pane["handle"]])
        try:
            _terminal_call(argv, root)
        except (OSError, subprocess.SubprocessError) as exc:
            remaining.append(pane)
            errors.append(str(exc))
    if record.get("url"):
        with suppress(OSError):
            with urlopen(Request(record["url"] + "stop", data=b"", method="POST"), timeout=2):
                pass
    store._write_json(path, {"panes": remaining})
    if errors:
        raise DesignError("Could not close preview pane(s); retry fa design done: " + "; ".join(errors))


def run(args, store: StateStore) -> int:
    root = args.repo_root.resolve()
    if args.action == "picks":
        print(json.dumps(picks(store, args.topic), ensure_ascii=False))
        return 0
    if args.action == "_serve":
        serve(store, args.directory, args.topic, args.token)
        return 0
    if args.action == "done":
        done(store, root)
        print("Design preview closed; the lane view remains available.")
        return 0
    commands, urls = args.preview_commands or [], args.url or []
    refs = None
    if args.stage == "board":
        if not args.references or commands or urls:
            raise DesignError("Board preview needs --references and no --command/--url")
        refs = _manifest(args.references.resolve())
    elif args.references or not 1 <= len(commands or urls) <= 3:
        raise DesignError("Draft preview needs one to three --command values or --url values")
    for url in urls:
        _web_url(url)
    if any(not command.strip() for command in commands):
        raise DesignError("Draft commands cannot be empty")
    done(store, root)
    session = {"panes": []}
    session_path = store.root / "design" / "session.json"
    if commands:
        terminal = watch_open._recorded_terminal(store, root)
        executable = shutil.which("orca") if os.environ.get("ORCA_TERMINAL_HANDLE") else None
        if not terminal and executable:
            terminal = executable, os.environ["ORCA_TERMINAL_HANDLE"]
        tmux = shutil.which("tmux") if os.environ.get("TMUX") else None
        if not terminal and not tmux:
            raise DesignError("Terminal previews need an existing Orca pane or tmux window")
        for command in commands:
            if terminal:
                executable, anchor = terminal
                result = _terminal_call([executable, "terminal", "split", "--terminal", anchor,
                                         "--direction", "vertical", "--command", command, "--json"], root)
                handle = _handle(json.loads(result.stdout))
                kind = "orca"
            else:
                result = _terminal_call([tmux, "split-window", "-h", "-P", "-F", "#{pane_id}",
                                         "-c", str(root), command], root)
                handle, kind, executable = result.stdout.strip(), "tmux", tmux
            if not handle:
                raise DesignError("Terminal opener returned no preview pane handle")
            session["panes"].append({"kind": kind, "executable": executable, "handle": handle})
            store._write_json(session_path, session)
            if terminal:
                terminal = executable, handle
    elif refs is not None or len(urls) > 1:
        token = secrets.token_urlsafe(24)
        directory = _topic(store, args.topic) / "previews" / token
        directory.mkdir(parents=True)
        if refs is not None:
            _board(store, directory, args.topic, refs, token)
        else:
            frames = "".join(f'<article><h2>Draft {i}</h2><iframe title="Draft {i}" '
                             f'src="{escape(url, quote=True)}"></iframe></article>' for i, url in enumerate(urls, 1))
            (directory / "index.html").write_text(_page(args.topic, f'<div class="grid">{frames}</div>'), encoding="utf-8")
        session["url"] = _open_page(store, directory, args, token)
        store._write_json(session_path, session)
        webbrowser.open(session["url"])
    else:
        webbrowser.open(urls[0])
    location = session.get("url") or (urls[0] if urls else None)
    if location:
        print(f"Preview URL: {location}")
    print(f"Design preview: {args.topic} ({args.stage}). Read choices with fa design picks; finish with fa design done.")
    return 0
