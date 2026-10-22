"""Local reference/comparison pages and their loopback-only HTTP transport."""

import json
import secrets
import shutil
import subprocess
from collections import defaultdict
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, unquote, urlsplit

from foragent.design import DesignError
from foragent.state import StateStore

STYLE = """
body { margin: 2rem; font: 16px system-ui, sans-serif; background: #f5f5f2; color: #202420; }
h1 { margin-bottom: .5rem; } section { margin-block: 2rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; }
article { background: white; padding: 1rem; border: 1px solid #ccc; border-radius: .6rem; }
img { width: 100%; height: 220px; object-fit: contain; background: #eee; }
button { font: inherit; cursor: pointer; padding: .5rem 1rem; margin-right: .5rem; }
button[aria-pressed=true] { outline: 3px solid #346a51; }
.drafts { display: grid; grid-template-columns: repeat(var(--count), minmax(0, 1fr)); gap: 1rem; }
iframe { width: 100%; height: 80vh; border: 1px solid #888; background: white; }
"""


def page(title: str, body: str) -> str:
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)}</title><style>{STYLE}</style></head>'
            f'<body><h1>{escape(title)}</h1>{body}</body></html>\n')


def web_url(value: str) -> str:
    try:
        location = urlsplit(value) if isinstance(value, str) else None
        if location is None or location.scheme not in {"http", "https"} or not location.hostname:
            raise ValueError("missing web host")
    except ValueError as exc:
        raise DesignError("reference and draft URLs must use http:// or https:// with a valid host") from exc
    return value


def read_references(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    references = data.get("references") if isinstance(data, dict) else None
    if not isinstance(references, list) or not references:
        raise DesignError("--references needs a JSON object with a nonempty references list")
    ids = set()
    for ref in references:
        if not isinstance(ref, dict) or any(not isinstance(ref.get(key), str) or not ref[key].strip()
                                            for key in ("id", "direction", "title", "url", "kind")):
            raise DesignError("each reference needs id, direction, title, url and kind strings")
        if ref["id"] in ids:
            raise DesignError(f"duplicate reference id: {ref['id']}")
        ids.add(ref["id"])
        web_url(ref["url"])
        if ref["kind"] not in {"web", "image"}:
            raise DesignError("reference kind must be web or image")
        if ref["kind"] == "image":
            image = ref.get("image")
            if not isinstance(image, str) or not image:
                raise DesignError(f"image reference {ref['id']} needs an image path")
            source = Path(image).expanduser()
            source = source if source.is_absolute() else path.parent / source
            if not source.is_file() or source.suffix.lower() not in {".png", ".gif", ".jpg", ".jpeg", ".webp"}:
                raise DesignError(f"reference image must be a local PNG, GIF, JPEG or WebP file: {source}")
            ref["image"] = str(source.resolve())
    return references


def screenshot(url: str, output: Path, root: Path) -> None:
    executable = shutil.which("playwright")
    if executable:
        argv = [executable, "screenshot"]
    elif executable := shutil.which("npx"):
        argv = [executable, "--no-install", "playwright", "screenshot"]
    else:
        raise DesignError("web references need Playwright CLI and Chromium; install them or supply kind=image screenshots")
    try:
        subprocess.run([*argv, "--full-page", url, str(output)], cwd=root,
                       capture_output=True, text=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesignError(f"could not screenshot {url}; check Playwright/Chromium or supply a local image: {exc}") from exc
    if not output.is_file():
        raise DesignError(f"screenshot tool did not create {output}")


def read_picks(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"likes": [], "dislikes": []}


def board_html(data: dict, picks: dict) -> str:
    groups = defaultdict(list)
    for ref in data["references"]:
        groups[ref["direction"]].append(ref)
    sections = []
    for direction, references in groups.items():
        cards = []
        for ref in references:
            buttons = "".join(
                f'<button name="vote" value="{vote}" aria-pressed="{str(ref["id"] in picks[vote]).lower()}">'
                f'{emoji}</button>' for vote, emoji in (("likes", "👍"), ("dislikes", "👎")))
            cards.append(
                f'<article><h3>{escape(ref["title"])}</h3>'
                f'<a href="{escape(ref["url"], quote=True)}" target="_blank" rel="noopener noreferrer">'
                f'<img src="{escape(ref["thumbnail"], quote=True)}" alt="{escape(ref["title"], quote=True)}"></a>'
                f'<form method="post" action="/{data["token"]}/picks">'
                f'<input type="hidden" name="id" value="{escape(ref["id"], quote=True)}">{buttons}</form></article>')
        sections.append(f'<section><h2>{escape(direction)}</h2><div class="grid">{"".join(cards)}</div></section>')
    return page(f'{data["topic"]} · Reference board',
                '<p>Open an original by clicking its thumbnail. Choose 👍 or 👎; selections are saved locally.</p>'
                + "".join(sections))


def prepare_board(store: StateStore, directory: Path, topic: str, references: list[dict], root: Path,
                  picks_path: Path) -> None:
    images = directory / "images"
    images.mkdir(parents=True)
    for index, ref in enumerate(references):
        suffix = Path(ref["image"]).suffix.lower() if ref["kind"] == "image" else ".png"
        output = images / f"{index}{suffix}"
        if ref["kind"] == "web":
            screenshot(ref["url"], output, root)
        else:
            shutil.copyfile(ref["image"], output)
        ref["thumbnail"] = f"images/{output.name}"
    data = {"topic": topic, "references": references, "picks_path": str(picks_path), "token": secrets.token_urlsafe(24)}
    store._write_json(directory / "board.json", data)
    store._atomic_write(directory / "index.html", board_html(data, read_picks(picks_path)))


def prepare_drafts(store: StateStore, directory: Path, topic: str, urls: list[str]) -> None:
    frames = "".join(f'<section><h2>Draft {index}</h2><iframe title="Draft {index}" '
                     f'src="{escape(url, quote=True)}"></iframe></section>' for index, url in enumerate(urls, 1))
    store._atomic_write(directory / "index.html", page(f"{topic} · Running drafts",
                        f'<main class="drafts" style="--count:{len(urls)}">{frames}</main>'))


def serve(store: StateStore, directory: Path) -> None:
    """Serve only the comparison page and copied images, never the repository."""
    directory = directory.resolve()
    if not directory.is_relative_to((store.root / "design").resolve()) or not (directory / "index.html").is_file():
        raise DesignError("design server directory must be a prepared local preview")
    token = secrets.token_urlsafe(24)
    data_path = directory / "board.json"
    data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.is_file() else None
    picks_path = Path(data["picks_path"]) if data else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _local(self) -> bool:
            try:
                host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            except ValueError:
                host = None
            if host not in {"localhost", "127.0.0.1", "::1"}:
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            if not self._local():
                return
            path = unquote(urlsplit(self.path).path)
            if path in {"/", "/index.html"}:
                file, content_type = directory / "index.html", "text/html; charset=utf-8"
            elif path.startswith("/images/") and Path(path).name in {
                Path(ref["thumbnail"]).name for ref in (data or {}).get("references", [])
            }:
                file = (directory / path.lstrip("/")).resolve()
                if not file.is_relative_to(directory / "images"):
                    self.send_error(404)
                    return
                content_type = {".png": "image/png", ".gif": "image/gif", ".jpg": "image/jpeg",
                                ".jpeg": "image/jpeg", ".webp": "image/webp"}[file.suffix]
            else:
                self.send_error(404)
                return
            try:
                content = file.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            if not self._local():
                return
            if self.path == f"/{token}/stop":
                self.send_response(204)
                self.end_headers()
                Thread(target=self.server.shutdown, daemon=True).start()
                return
            if data is None or self.path != f'/{data["token"]}/picks':
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("invalid form size")
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                reference_id, vote = fields["id"][0], fields["vote"][0]
                if reference_id not in {ref["id"] for ref in data["references"]} or vote not in {"likes", "dislikes"}:
                    raise ValueError("invalid selection")
            except (KeyError, ValueError, UnicodeError):
                self.send_error(400)
                return
            # Votes must finish even while preview/done waits for this server.
            with StateStore(picks_path.parent)._state_lock():
                picks = read_picks(picks_path)
                for key in ("likes", "dislikes"):
                    picks[key] = sorted(set(picks[key]) - {reference_id})
                picks[vote].append(reference_id)
                picks[vote].sort()
                store._write_json(picks_path, picks)
                store._atomic_write(directory / "index.html", board_html(data, picks))
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        store._write_json(directory / "ready.json", {"url": url, "stop_url": f"{url}{token}/stop"})
        server.serve_forever()
    finally:
        server.server_close()
