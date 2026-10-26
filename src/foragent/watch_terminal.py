"""Curses painting, background refresh and lane keyboard controls.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from foragent.config import Config
from foragent.watch_data import Snapshot


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

    styles = {"bold": curses.A_BOLD, "edge": curses.A_BOLD}
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
    screen.keypad(True)
    if store is not None:
        try:
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
        except curses.error:
            pass  # A terminal without mouse support keeps the keyboard view.
    screen.timeout(300)
    selected = None
    show_detail = False
    offset = 0
    digits = ""
    error = ""
    animation_started = watch.time.monotonic()
    next_refresh = animation_started + 5
    pending = None
    started_build = watch._installed_build()
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
                frame = watch._frame(snapshot, config, repo_root, tick=tick, width=max(40, width - 1), selected=selected,
                                     now=now(), **({} if store is None else {"layout": layout}))
            except ImportError:
                # A reinstall can briefly leave modules missing; restart on the installed code.
                return _restart(cleanup)
            # Keep keyboard help on screen even when there are more lanes than rows.
            body, footer = frame[:-1], frame[-1]
            if error:
                footer = watch._line(error, "yellow")
            if show_detail:
                offset = len(body)
                show_detail = False
            offset = max(0, min(offset, len(body) - max(1, height - 1)))
            visible = body[offset:offset + max(0, height - 1)]
            screen.erase()
            for y, line in [*enumerate(visible), (height - 1, footer)]:
                x = 0
                for value, style in watch._fit(line, max(0, width - 1)):
                    try:
                        screen.addstr(y, x, value, styles.get(style, 0))
                    except curses.error:
                        pass  # A resize may occur between measuring and painting.
                    x += watch._cells(value)
            screen.refresh()
            key = screen.getch()
            slugs = list(snapshot.status["lanes"])
            if key in (ord("q"), ord("Q"), 27):
                return
            if key == curses.KEY_MOUSE and store is not None:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue
                if y < height - 1 and buttons & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    watch.handle_mouse("left", x, offset + y, layout, store, config)
                continue
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                offset += 1 if key == curses.KEY_DOWN else max(1, height - 2)
            elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                offset -= 1 if key == curses.KEY_UP else max(1, height - 2)
            elif key in range(ord("0"), ord("9") + 1):
                digits = (digits + chr(key))[-len(str(len(slugs))):]
                if len(slugs) > 9:
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
                        offset = 0
