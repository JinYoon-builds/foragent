"""Shared world and readers for the member-card acceptance tests (not a test module).

The world is one repository with two lanes: ``api`` has every fact the card names
(configuration, run history with skill reads, merges and review rejections today and
yesterday in local time, a current issue and a lane memory), and ``quiet`` has none of
them. Every value is distinctive so that its presence in the card proves where it came from.

The card is read from the squirrel's speech bubble without assuming its height: every
non-blank body row between the bubble's top and bottom borders is one card row. The real
terminal session clicks with mouse bytes and reads what the terminal drew after the click.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import re
import select
import shutil
import time

# 2026-10-20 09:00 UTC is 18:00 in Seoul, so "today" starts at 2026-10-19 15:00 UTC.
LOCAL_ZONE = "Asia/Seoul"
UTC = timezone.utc

PATHS = ("src/api/**", "lib/apicore/**")
EXECUTOR, MODEL = "claude", "opus-quokka-9"
FIXED_SKILLS = ("skill-fixed-alpha", "skill-fixed-beta")
USED_SKILL = "skill-used-gamma"
OLD_LESSON = "Older lesson about walrus-tusks."
NEW_LESSON = "Newest lesson about quokka-77 hops."
OUTSIDE_LESSON = "Outside lesson about marmot-13."
FOLDER = ".claude/agent-memory/fa-api"
QUIET_PATHS = ("quiet/**",)
CURRENT_MINUTES = 37

NONE_WORDS = re.compile(r"(?i)\bunknown\b|\bnone\b|\bno\b|\bnot\b|—|\bn/a\b")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def local_today():
    """Pin the process time zone so "today" is counted in a known local time.

    The test module's ``restored_zone`` fixture puts the old zone back afterwards.
    """
    os.environ["TZ"] = LOCAL_ZONE
    time.tzset()


def _claude_skill_read(name):
    """A Claude log showing one successful read of a skill file."""
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read-1", "name": "Read",
         "input": {"file_path": f".claude/skills/{name}/SKILL.md"}}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "read-1", "content": "skill text"}]}}
    return json.dumps(use) + "\n" + json.dumps(result) + "\n"


def _merged_run(case, number, start, end, *, skills=(), title=None):
    case.tracker.add_issue(number, title or f"Merged change {number}", labels=["lane:api"], state="closed")
    case.clock[0] = start
    case.store.start_run(number, branch=f"foragent/issue-{number}",
                         worktree=case.store.worktree_path(number), lane="api")
    if skills:
        log = case.store.log_path(number)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(_claude_skill_read(name) for name in skills), encoding="utf-8")
        case.store.update_run(number, skills_used=list(skills), skills_reported=True)
    case.clock[0] = end
    case.store.finish_run(number, "merged")


def _rejection(case, number, moment):
    from foragent import orch_events

    orch_events.append_event(case.store, moment, number, "review", "FAIL 2 findings",
                             verdict="FAIL", findings=2, round=1, source="reviewer")


def member_world(git_history, tmp_path, monkeypatch, *, language="en", extra_merge=False,
                 extra_rejection=False, memory="normal", minutes=CURRENT_MINUTES, current=True):
    """The two-lane repository after one gate tick, with the CLI bound to it.

    Today (local): #11 merged at 05:00 UTC with one rejection, #12 merged at 15:30 UTC the
    day before (still today in Seoul). Yesterday (local): #13 merged at 14:30 UTC with one
    rejection. ``extra_merge`` adds #14 merged today, older than the newest merge;
    ``extra_rejection`` adds a rejection of #12 today, older than the newest rejection.
    ``memory`` is "normal", "missing" (no folder) or "outside" (the folder links outside the
    repository); ``current=False`` stops #1, so the lane has no current issue.
    """
    from foragent.scheduler import tick
    from test_orch_review_handoff import bind_cli, make_case, tick_context

    local_today()
    lanes = {
        "api": {"paths": list(PATHS), "executor": EXECUTOR, "model": MODEL,
                "skills": list(FIXED_SKILLS), "agent": "fa-api"},
        "quiet": {"paths": list(QUIET_PATHS), "executor": "codex"},
    }
    case = make_case(git_history, tmp_path, monkeypatch, language=language, skill_libraries=[],
                     test_command="true", lanes=lanes)
    now = at(20, 9) + timedelta(minutes=minutes)
    _merged_run(case, 13, at(19, 14), at(19, 14, 30), skills=("skill-yesterday-delta",))
    _rejection(case, 13, at(19, 14, 10))
    _merged_run(case, 12, at(19, 15, 10), at(19, 15, 30))
    if extra_merge:
        _merged_run(case, 14, at(19, 16), at(19, 16, 20))
    if extra_rejection:
        _rejection(case, 12, at(19, 15, 20))
    _merged_run(case, 11, at(20, 4, 40), at(20, 5), skills=(USED_SKILL,))
    _rejection(case, 11, at(20, 4, 50))
    case.clock[0] = at(20, 9)
    if current:
        # make_case started its run at 09:00 UTC; it becomes this lane's work.
        case.tracker.add_labels(1, ["lane:api"])
        case.store.update_run(1, lane="api", phase="implementing")
    else:
        case.clock[0] = at(20, 9, 1)
        case.store.finish_run(1, "stopped")
        case.tracker.close_issue(1)
        case.tracker.remove_labels(1, ["running"])
    case.clock[0] = now
    folder = case.root / FOLDER
    # Worlds share one clone: start every memory case from no lane memory at all.
    if folder.is_symlink() or folder.is_file():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)
    if memory == "normal":
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "MEMORY.md").write_text(f"# API lane memory\n\n- {OLD_LESSON}\n- {NEW_LESSON}\n",
                                          encoding="utf-8")
    elif memory == "outside":
        outside = tmp_path / "outside-memory"
        outside.mkdir()
        (outside / "MEMORY.md").write_text(f"# Elsewhere\n\n- {OUTSIDE_LESSON}\n", encoding="utf-8")
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.symlink_to(outside, target_is_directory=True)
    main = bind_cli(case, monkeypatch)
    tick(tick_context(case, start_runs=False))
    return case, main


# ---------------------------------------------------------------- reading the card


def bubble_rows(frame):
    """The speech bubble's body rows, however tall the bubble has grown.

    The bubble is the first box drawn with rounded corners; its body is every row between
    the top border and the next bottom border, inside the left edge (or its mouth) and the
    right edge. Blank rows are dropped.
    """
    lines = frame.splitlines()
    first = next((i for i, line in enumerate(lines) if "\u256d" in line and "\u256e" in line), None)
    assert first is not None, f"the dashboard draws the squirrel's speech bubble:\n{frame}"
    last = next((i for i in range(first + 1, len(lines)) if "\u2570" in lines[i]), None)
    assert last is not None, f"the speech bubble is closed by a bottom border:\n{frame}"
    rows = []
    for index in range(first + 1, last):
        line = lines[index]
        edges = [i for i, ch in enumerate(line) if ch in "\u2502\u2524"]
        if len(edges) < 2:
            continue
        text = line[edges[0] + 1:line.rindex("\u2502")].strip()
        if text:
            rows.append((index, text))
    return rows


def dashboard_card(case, main, capsys, lane, *, width=200):
    """`fa gate watch --once`'s own snapshot, clicked on the lane's squirrel.

    Returns (bubble rows before, frame after, card rows, view state): the card rows are the
    speech bubble's body after the click. The click goes through the public
    render_frame/handle_mouse pair, as the live view does.
    """
    import pytest
    from foragent import watch
    from test_acorn_interact import hit_positions
    from test_squirrel_running import plain_frame

    seen, real = {}, watch.render_frame

    def capture(snapshot, config, root, **kwargs):
        seen.update(snapshot=snapshot, config=config, root=root)
        return real(snapshot, config, root, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(watch, "render_frame", capture)
        assert main(["gate", "watch", "--once", "--repo-root", str(case.root)]) == 0
    capsys.readouterr()
    state, layout = {"store": case.store}, {}

    def draw(into=None):
        return plain_frame(real(seen["snapshot"], seen["config"], seen["root"], width=width, state=state,
                                layout={} if into is None else into, now=case.clock[0]))

    before = draw(layout)
    hits = hit_positions(layout, before, width)
    assert ("squirrel", lane) in hits, f"the {lane} lane keeps a clickable squirrel"
    watch.handle_mouse("left", *hits[("squirrel", lane)], layout, case.store, seen["config"], state=state)
    after = draw()
    return bubble_rows(before), after, bubble_rows(after), state


def card_text(rows):
    return "\n".join(text for _, text in rows)


def numbers(text):
    from collections import Counter

    return Counter(re.findall(r"(?<![\w.#-])\d+(?![\w.-])", text))


# ---------------------------------------------------------------- a real terminal session


def _mouse_press(raw, x, y):
    """A left press and release at cell (x, y), in the protocol the app turned on."""
    if re.search(rb"\x1b\[\?(?:[0-9]+;)*1006(?:;[0-9]+)*h", raw):
        return f"\x1b[<0;{x + 1};{y + 1}M".encode(), f"\x1b[<0;{x + 1};{y + 1}m".encode()
    return b"\x1b[M" + bytes([32, 33 + x, 33 + y]), b"\x1b[M" + bytes([35, 33 + x, 33 + y])


def clicked_session(main, root, target, hits_file, *, rows=30, columns=120, limit=40.0):
    """Run `fa gate watch` in a pty, left-click ``target`` with real mouse bytes, then press q.

    The child records the live view's own hit boxes (the layout of the frame it last
    painted) into ``hits_file`` so the parent knows where the target is on screen; nothing
    in the input path is replaced. Returns the text the terminal drew after the click.
    Only this child is waited for; no signal is sent to anything.
    """
    import fcntl
    import pty
    import struct
    import termios

    from helpers.terminal_view import _screen_text

    pid, fd = pty.fork()
    if pid == 0:
        code = 1
        try:
            import sys

            from foragent import watch

            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = open(1, "w", encoding="utf-8", closefd=False, buffering=1)
            sys.stderr = open(2, "w", encoding="utf-8", closefd=False, buffering=1)
            painted, frame = [], watch._frame

            def observed(*args, **kwargs):
                # The previous frame's layout is final (screen rows) once the next frame starts.
                if painted and isinstance(painted[-1], dict) and painted[-1].get("hits"):
                    boxes = [[x0, y0, x1, y1, list(t) if isinstance(t, tuple) else t]
                             for x0, y0, x1, y1, t in painted[-1]["hits"]]
                    partial = hits_file.with_suffix(".partial")
                    partial.write_text(json.dumps(boxes, default=str), encoding="utf-8")
                    os.replace(partial, hits_file)
                painted[:] = [kwargs.get("layout")]
                return frame(*args, **kwargs)

            watch._frame = observed
            code = main(["gate", "watch", "--repo-root", str(root)]) or 0
        except BaseException:  # noqa: BLE001  (the parent reports a non-zero exit)
            import traceback
            traceback.print_exc()
        finally:
            os._exit(code)
    chunks, deadline = [], time.monotonic() + limit
    clicked_at = clicked_index = quit_index = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            raw = b"".join(chunks)
            if clicked_at is None and b"\x1b[?1049h" in raw and hits_file.exists():
                boxes = json.loads(hits_file.read_text(encoding="utf-8"))
                found = [box for box in boxes if box[4] == list(target)]
                if found:
                    x0, y0, x1, y1, _ = found[0]
                    press, release = _mouse_press(raw, x0, y0)
                    clicked_index = len(chunks)
                    os.write(fd, press)
                    os.write(fd, release)
                    clicked_at = time.monotonic()
            if clicked_at is not None and quit_index is None and time.monotonic() >= clicked_at + 2.5:
                quit_index = len(chunks)
                os.write(fd, b"q")
    finally:
        os.close(fd)
        status, waited = None, time.monotonic() + 10
        while time.monotonic() < waited:
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
                break
            time.sleep(0.1)
    raw = b"".join(chunks)
    assert status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, \
        _screen_text(raw)[-2000:]
    assert b"\x1b[?1049h" in raw, "the live view must open on the alternate screen"
    assert clicked_at is not None, (
        f"the live view never painted a clickable {target!r}: {_screen_text(raw)[-1500:]}")
    assert quit_index is not None, "the session ended before the click was answered"
    return _screen_text(b"".join(chunks[clicked_index:quit_index]))
