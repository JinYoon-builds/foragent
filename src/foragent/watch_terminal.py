"""Curses painting, background refresh and lane keyboard controls.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
import re

from foragent.config import Config
from foragent.watch_data import Snapshot


# A visible issue number, linked to its GitHub page after each refresh.
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
            curses.KEY_PPAGE: "pageup", 27: "esc", **{ord(key): key for key in "jkfaoxqQ"}}
    last_painted = None
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
            layout: dict = {}
            try:
                # The terminal's own width, so the 60- and 100-column rules hold as in a plain frame.
                # The live view keeps every section and scrolls the whole frame.
                frame = watch._frame(snapshot, config, repo_root, tick=tick, width=max(1, width), selected=selected,
                                     now=now(), **({} if store is None else {"layout": layout}), height=height,
                                     state=view_state, scrolls=True)
            except ImportError:
                # A reinstall can briefly leave modules missing; restart on the installed code.
                return _restart(cleanup)
            if show_detail:
                view_state["scroll"] = len(frame)  # Jump to the lane details at the end.
                show_detail = False
            visible = watch._window(frame, view_state, max(1, height), max(1, width), config.language)
            # A frame that fits keeps its key help on the bottom row.
            if len(frame) <= height and visible:
                visible = visible[:-1] + [[]] * (height - len(visible)) + visible[-1:]
            if error:
                visible = visible[:-1] + [watch._fit(watch._line(error, "yellow"), max(1, width))]
            top = view_state.get("scroll", 0)
            mark = watch.highlighted(view_state) if view_state.get("issue") is None else None
            marked = {y - top for y, number in view_state.get("rows", ()) if number == mark}
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
            # Curses cannot write OSC 8: each visible #n is written again as a link in place,
            # whenever curses may have repainted it.
            painted = (links, [[value for value, _ in line] for line in visible], sorted(marked))
            if links and painted != last_painted:
                watch.sys.stdout.write("".join(links))
                watch.sys.stdout.flush()
            last_painted = painted
            key = screen.getch()
            slugs = list(snapshot.status["lanes"])
            if key == curses.KEY_MOUSE and store is not None:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue
                if buttons & wheel[0]:
                    watch.handle_mouse("wheel-up", x, top + y, layout, store, config, state=view_state)
                elif buttons & wheel[1]:
                    watch.handle_mouse("wheel-down", x, top + y, layout, store, config, state=view_state)
                elif buttons & click and y < len(visible) - (len(frame) > height):
                    watch.handle_mouse("left", x, top + y, layout, store, config, state=view_state)
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
                if store is not None:
                    watch.handle_key(keys[key], store, config, state=view_state)
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
