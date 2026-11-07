"""Acceptance tests for footer-settings: wherever s opens settings, the footer names it.

Statement: every dashboard view in which pressing s opens the settings window shows the s
settings hint in its footer, in the language in use, whatever state the view's facts are in.

The live dashboard draws the gate's now-snapshot (now.json). On main its footer reads only
"click an acorn · q quit", although s opens settings there.
"""

from datetime import timedelta
import os
import select
import time

import pytest

from helpers.terminal_view import _screen_text, offline_terminal, isolated_home  # noqa: F401  (autouse)

HINT = {"ko": "s 설정", "en": "s settings"}
WINDOW = {"ko": ("팀", "나", "이 컴퓨터"), "en": ("Team", "Me", "This computer")}


def _plain(frame):
    import re

    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", frame)


def _case(git_history, tmp_path, monkeypatch, language):
    """A repository with working, blocked, question and dependency-waiting lanes, as status_case."""
    from test_view_status import status_case

    case, main = status_case(git_history, tmp_path, monkeypatch, language=language)
    case.store.update_run(1, pid=os.getpid(), phase="implementing",
                          last_activity_at=(case.clock[0] - timedelta(seconds=30)).isoformat())
    return case, main


def _tick(case):
    from foragent.scheduler import tick
    from test_orch_review_handoff import tick_context

    tick(tick_context(case, start_runs=False))


def _footer_ok(frame, language):
    lines = [line.rstrip() for line in _plain(frame).splitlines() if line.strip()]
    assert lines, "the dashboard printed nothing"
    footer = lines[-1]
    assert HINT[language] in footer, f"the footer must name the s settings key: {footer!r}"
    assert footer.count(HINT[language]) == 1, f"the s hint is named once: {footer!r}"
    other = HINT["en" if language == "ko" else "ko"]
    assert other not in footer, f"the hint follows the language in use: {footer!r}"
    assert sum(HINT[language] in line for line in lines) == 1, "exactly one footer names the key"
    return footer


def _assert_s_opens_settings(snapshot, config, case, language):
    """The same view: s opens the settings window (so the footer must name it)."""
    from foragent import watch

    state = {"store": case.store}
    assert watch.handle_key("s", case.store, config, state=state, repo_root=case.root) is False
    opened = _plain(watch.render_frame(snapshot, config, case.root, width=160, state=state))
    assert all(label.casefold() in opened.casefold() for label in WINDOW[language]), opened


# ---- the printed dashboard (fa gate watch --once / fa dashboard --once) ----------------------

@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("command", [["gate", "watch"], ["dashboard"]])
def test_published_dashboard_footer_names_the_settings_key(git_history, tmp_path, monkeypatch, capsys,
                                                         language, command):
    from foragent import watch

    case, main = _case(git_history, tmp_path, monkeypatch, language)
    _tick(case)
    seen, real = {}, watch.render_frame

    def capture(snapshot, config, root, **kwargs):
        seen.update(snapshot=snapshot, config=config)
        return real(snapshot, config, root, **kwargs)

    monkeypatch.setattr(watch, "render_frame", capture)
    capsys.readouterr()
    assert main([*command, "--once", "--repo-root", str(case.root)]) == 0
    printed = capsys.readouterr().out
    assert seen["snapshot"].now_view is not None, "the dashboard draws the gate's now-snapshot"
    _footer_ok(printed, language)
    _assert_s_opens_settings(seen["snapshot"], seen["config"], case, language)


@pytest.mark.parametrize("language", ["ko", "en"])
def test_footer_names_the_settings_key_on_an_empty_repository(git_history, tmp_path, monkeypatch, capsys,
                                                            language):
    from test_orch_review_handoff import bind_cli, make_case

    case = make_case(git_history, tmp_path, monkeypatch, language=language)
    main = bind_cli(case, monkeypatch)
    _tick(case)
    capsys.readouterr()
    assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    _footer_ok(capsys.readouterr().out, language)


# ---- the now-snapshot reader in every fact state ----------------------------------------------

@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("facts", ["published", "unreadable", "missing", "explained", "narrow-lanes"])
def test_now_view_footer_names_the_settings_key_in_every_fact_state(git_history, tmp_path, monkeypatch,
                                                                   language, facts):
    from foragent import watch, watch_now

    case, _ = _case(git_history, tmp_path, monkeypatch, language)
    _tick(case)
    path = case.store.root / watch_now.FILENAME
    assert path.exists(), "the tick publishes now.json"
    if facts == "unreadable":
        path.write_text("{not json", encoding="utf-8")
    elif facts == "missing":
        path.unlink()
    config = case.ctx.config
    assert config.language == language
    snapshot = watch_now.read(case.store, case.clock[0], language)
    assert snapshot.now_view is not None, "the live dashboard always reads the now-snapshot"
    state, layout = {"store": case.store}, {}
    width = 120 if facts == "narrow-lanes" else 160
    frame = watch.render_frame(snapshot, config, case.root, width=width, state=state, layout=layout,
                               now=case.clock[0])
    if facts == "explained":
        targets = [hit[4] for hit in layout.get("hits", ()) if hit[4][0] == "acorn"]
        assert targets, "the published dashboard has a clickable acorn"
        state["explanation_target"] = targets[0]
        frame = watch.render_frame(snapshot, config, case.root, width=width, state=state, layout={},
                                   now=case.clock[0])
    _footer_ok(frame, language)
    _assert_s_opens_settings(snapshot, config, case, language)


# ---- a real terminal session ------------------------------------------------------------------

def _pty_session(main, root, keys_after_frame):
    """Run `fa gate watch` in a 120x30 pty; after the first frame type each key in turn."""
    import fcntl
    import pty
    import struct
    import termios

    pid, fd = pty.fork()
    if pid == 0:
        code = 1
        try:
            import sys
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False, buffering=1)
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False, buffering=1)
            code = main(["gate", "watch", "--repo-root", str(root)]) or 0
        except BaseException:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        finally:
            os._exit(code)
    chunks, marks, pending = [], [], list(keys_after_frame)
    deadline, next_at = time.monotonic() + 40, None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            if next_at is None and b"\x1b[?1049h" in b"".join(chunks):
                next_at = time.monotonic() + 3.0
            if next_at is not None and pending and time.monotonic() >= next_at:
                marks.append(len(b"".join(chunks)))
                os.write(fd, pending.pop(0))
                next_at = time.monotonic() + 2.0
    finally:
        # No signal is ever sent: an unfinished session gets q, the view's own quit key.
        if pending:
            try:
                os.write(fd, b"q")
            except OSError:
                pass
        status, waited = None, time.monotonic() + 10
        while status is None and time.monotonic() < waited:
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
            else:
                try:
                    select.select([fd], [], [], 0.2) and os.read(fd, 65536)
                except OSError:
                    time.sleep(0.2)
        os.close(fd)
        if status is None:
            _, status = os.waitpid(pid, 0)  # Closing the terminal ends the dashboard it started.
    raw = b"".join(chunks)
    assert not pending, "the dashboard did not reach its first frame in time"
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, _screen_text(raw)[-2000:]
    bounds = [0, *marks, len(raw)]
    return [_screen_text(raw[a:b]) for a, b in zip(bounds, bounds[1:])]


@pytest.mark.parametrize("language", ["ko", "en"])
def test_real_terminal_footer_names_s_and_s_opens_settings(git_history, tmp_path, monkeypatch, language):
    case, main = _case(git_history, tmp_path, monkeypatch, language)
    _tick(case)
    before, after_s, _ = _pty_session(main, case.root, [b"s", b"q"])
    assert HINT[language] in before, "the first live frame's footer names the s settings key"
    assert all(label in after_s for label in WINDOW[language][:1]), "s opens the settings window"
