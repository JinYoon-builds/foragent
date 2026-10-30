"""Curses painting, background refresh and lane keyboard controls.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
import re

from foragent.config import Config
from foragent.watch_data import Snapshot
from foragent.watch_sections import talk as talk_section


# A visible issue number, linked to its GitHub page after each refresh.
SAVE_CURSOR, RESTORE_CURSOR = "\x1b7", "\x1b8"
ISSUE_NUMBER = re.compile(r"(?<![\w#])#(\d+)(?!\w)")


def color_pairs(colors: int) -> list[tuple[int, int]]:
    """Use the terminal's default text/background with base ANSI accents.

    The public API accepts the terminal's color count, but 8- and 256-color
    terminals deliberately share this palette; extended colors aren't needed.
    """
    import curses

    return [(-1, -1), (curses.COLOR_CYAN, -1),
            (curses.COLOR_GREEN, -1), (-1, -1),
            (curses.COLOR_MAGENTA, -1), (curses.COLOR_GREEN, -1),
            (curses.COLOR_BLUE, -1)]


def _restart(cleanup: ExitStack) -> bool:
    """Ask for a re-exec without joining the worker: a stuck refresh must not block it."""
    cleanup.pop_all()  # execv replaces the process, so the worker thread needs no shutdown.
    return True


def window_key(key: int, names: dict[int, str], prompting: bool, not_keys: tuple[int, ...]) -> str | None:
    """The settings window's name for one getch value, or None to ignore it.

    During the team y/n prompt a key outside the map (Space, a letter, F1) still answers:
    it counts as n. No key (a timeout), a resize or a mouse event is not a key.
    """
    name = names.get(key)
    if name is None and prompting and key not in not_keys:
        return "other"
    return name


def _reload(config: Config, repo_root: Path) -> Config:
    """The configuration as just saved, so rows, glyphs and the last line change at once."""
    from foragent.config import ConfigError, load_config

    try:
        return load_config(repo_root)
    except (ConfigError, OSError):
        return config


def _terminal(screen, refresh: Callable[[], Snapshot], snapshot: Snapshot, config: Config, repo_root: Path, now,
              store=None):
    from foragent import watch

    import curses

    styles = {"bold": curses.A_BOLD, "edge": curses.A_BOLD, "dim": curses.A_DIM}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for index, (foreground, background) in enumerate(watch.color_pairs(curses.COLORS), 1):
            curses.init_pair(index, foreground, background)
        styles.update(road=curses.color_pair(1), edge=curses.color_pair(1) | curses.A_BOLD,
                      divider=curses.color_pair(2), yellow=curses.color_pair(2),
                      green=curses.color_pair(3), queue=curses.color_pair(4),
                      hit=curses.color_pair(4) | curses.A_BOLD, claude=curses.color_pair(5),
                      codex=curses.color_pair(6), blue=curses.color_pair(7))
        screen.bkgd(" ", styles["road"])
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.set_escdelay(25)  # Esc closes a box at once instead of waiting for a sequence.
    except (curses.error, AttributeError):
        pass
    screen.keypad(True)
    wheel = getattr(curses, "BUTTON4_PRESSED", 0), getattr(curses, "BUTTON5_PRESSED", 0)
    click = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED
    if store is not None:
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)  # Presses arrive at once; a wheel step is never a click.
        except curses.error:
            pass  # A terminal without mouse support keeps the keyboard view.
    screen.timeout(300)
    selected = None
    show_detail = False
    digits = ""
    error = ""
    animation_started = watch.time.monotonic()
    next_refresh = animation_started + 5
    pending = None
    started_build = watch._installed_build()
    # Memory across frames: fireworks start times, folds, scroll and the open issue box.
    view_state: dict = {}
    if store is not None:
        view_state["store"] = store
    keys = {curses.KEY_DOWN: "down", curses.KEY_UP: "up", curses.KEY_NPAGE: "pagedown",
            curses.KEY_PPAGE: "pageup", 27: "esc", **{ord(key): key for key in "jkfaomxqQs"}}
    # The settings window's own keys (#173): there digits switch tabs instead of picking a lane.
    window_keys = {**keys, 10: "enter", 13: "enter", curses.KEY_ENTER: "enter", 9: "tab",
                   getattr(curses, "KEY_BTAB", 353): "btab", curses.KEY_LEFT: "left",
                   curses.KEY_RIGHT: "right", **{ord(key): key for key in "yYnN123"}}
    with ExitStack() as cleanup:
        worker = cleanup.enter_context(watch.ThreadPoolExecutor(max_workers=1))
        while True:
            if pending is not None and pending.done():
                try:
                    snapshot = pending.result()
                    error = ""
                except Exception as exc:
                    error = watch.text("watch_refresh_failed", config.language, error=watch._clean(exc))
                pending = None
                next_refresh = watch.time.monotonic() + 5
                if watch.should_reload(started_build, watch._installed_build()):
                    return _restart(cleanup)
            if watch.time.monotonic() >= next_refresh:
                if pending is None:
                    pending = worker.submit(refresh)
                else:
                    # A slow snapshot must not hide a reinstall: check once per due refresh.
                    next_refresh = watch.time.monotonic() + 5
                    if watch.should_reload(started_build, watch._installed_build()):
                        return _restart(cleanup)
            tick = int((watch.time.monotonic() - animation_started) / 0.3)
            height, width = screen.getmaxyx()
            view_state["height"] = height  # The settings window fits itself to this screen.
            layout: dict = {}
            try:
                # The terminal's own width, so the 60- and 100-column rules hold as in a plain frame.
                # F is built as render_frame builds it (the fixed 24-row trimming, also on a
                # tall terminal); the screen then shows render_frame's slice of it.
                frame = watch._frame(snapshot, config, repo_root, tick=tick, width=max(1, width), selected=selected,
                                     now=now(), **({} if store is None else {"layout": layout}), state=view_state)
            except ImportError:
                # A reinstall can briefly leave modules missing; restart on the installed code.
                return _restart(cleanup)
            if show_detail:
                view_state["scroll"] = len(frame)  # Jump to the lane details at the end.
                show_detail = False
            # The same slice as render_frame with this height; hit boxes move to screen rows.
            # Under 3 rows this is F itself, painted from the top and cut to the screen below.
            visible = watch._window(frame, view_state, height, max(1, width), config.language,
                                    layout if store is not None else None)
            top = view_state.get("scroll", 0)
            alert = [watch._fit(watch._line(error, "yellow"), max(1, width))] if error else []
            # A squirrel's talk result off this screen (short or scrolled) takes the bottom row.
            alert = alert or talk_section.alert(view_state, visible, max(1, width))
            if height >= watch.MIN_SCREEN and len(frame) <= height and visible:
                # F fits: painted as today, its last row (the key help) on the bottom row,
                # where a refresh error takes its place.
                body = visible[:-1]
                visible = body + [[]] * (height - 1 - len(body)) + (alert or visible[-1:])
            elif alert:
                # A refresh error takes the bottom row (in place of the position line).
                bottom = max(0, height - 1)
                visible = visible[:bottom] + [[]] * (bottom - len(visible[:bottom])) + alert
            # One highlighted row, by its position: the first issue row on screen.
            mark = watch.highlighted_row(view_state) if view_state.get("issue") is None else None
            marked = set() if mark is None else {mark - top}
            screen.erase()
            links: list[str] = []
            for y, line in enumerate(visible[:max(1, height)]):
                x = 0
                # _frame already cut every line to the width; it is painted as it is.
                for value, style in line:
                    attribute = styles.get(style, 0) | (curses.A_REVERSE if y in marked else 0)
                    try:
                        screen.addstr(y, x, value, attribute)
                    except curses.error:
                        # Writing the bottom-right cell reports an error after drawing it;
                        # a resize between measuring and painting does too.
                        pass
                    for found in ISSUE_NUMBER.finditer(value):
                        url = watch.issue_url(config, int(found[1]))
                        if url is not None:
                            look = ("\x1b[2m" if style == "dim" else "") + ("\x1b[7m" if y in marked else "")
                            links.append(f"\x1b[{y + 1};{x + watch._cells(value[:found.start()]) + 1}H{look}"
                                         + watch.osc8(url, found[0]) + ("\x1b[0m" if look else ""))
                    x += watch._cells(value)
            screen.refresh()
            # Curses cannot write OSC 8, so every visible #n is written again as a link after
            # each refresh. No "unchanged" shortcut: curses also repaints unchanged text (after
            # a resize, or when it clears the screen itself), and that drops the links.
            if links:
                # Save and restore the cursor and attributes (DECSC/DECRC) around the links,
                # so curses' next refresh starts from the position it believes it left.
                watch.sys.stdout.write(SAVE_CURSOR + "".join(links) + RESTORE_CURSOR)
                watch.sys.stdout.flush()
            key = screen.getch()
            if key not in (-1, curses.KEY_MOUSE, curses.KEY_RESIZE):
                view_state.pop("talk_message", None)  # A squirrel's talk line stays until the next key.
            slugs = list(snapshot.status["lanes"])
            if view_state.get("settings"):
                # The window is keyboard-only and takes every key but q.
                name = window_key(key, window_keys, view_state.get("settings_prompt") is not None,
                                  (-1, curses.KEY_RESIZE, curses.KEY_MOUSE))
                if name in ("q", "Q"):
                    return
                if name is not None:
                    watch.handle_key(name, store, config, state=view_state, repo_root=repo_root)
                if view_state.pop("config_changed", False):
                    config = _reload(config, repo_root)
                    if hasattr(refresh, "config"):
                        refresh.config = config  # Later snapshots read the saved settings too.
                    next_refresh = watch.time.monotonic()  # And the next one starts now.
                continue
            if key == curses.KEY_MOUSE and store is not None:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue
                # The hit boxes are in screen rows already: clicks pass through unchanged.
                if buttons & wheel[0]:
                    watch.handle_mouse("wheel-up", x, y, layout, store, config, state=view_state)
                elif buttons & wheel[1]:
                    watch.handle_mouse("wheel-down", x, y, layout, store, config, state=view_state)
                elif buttons & click:
                    watch.handle_mouse("left", x, y, layout, store, config, state=view_state)
                continue
            if key in range(ord("0"), ord("9") + 1):
                digits = (digits + chr(key))[-len(str(len(slugs))):]
                if len(slugs) > 9:
                    continue
            elif key in (10, 13, curses.KEY_ENTER) and not digits:
                if store is not None:
                    watch.handle_key("enter", store, config, state=view_state)
                continue
            elif key in keys:
                if keys[key] in ("q", "Q") or (keys[key] == "esc" and view_state.get("issue") is None):
                    return
                if store is not None or keys[key] == "s":
                    watch.handle_key(keys[key], store, config, state=view_state, repo_root=repo_root)
                elif keys[key] in ("down", "up", "j", "k", "pagedown", "pageup"):
                    step = 1 if keys[key] in ("down", "up", "j", "k") else max(1, height - 2)
                    watch.scroll(view_state, -step if keys[key] in ("up", "k", "pageup") else step)
                continue
            elif key not in (10, 13, curses.KEY_ENTER):
                continue
            if digits:
                number = int(digits)
                digits = ""
                if 1 <= number <= len(slugs):
                    slug = slugs[number - 1]
                    selected = None if selected == slug else slug
                    show_detail = selected is not None
                    if selected is None:
                        view_state["scroll"] = 0
