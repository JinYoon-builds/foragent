"""Curses painting, background refresh and lane keyboard controls.

Runtime dependencies resolve through foragent.watch to preserve its patch points.
"""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
import re

from foragent.config import Config
from foragent.watch_cells import Style
from foragent.watch_data import Snapshot


# A visible issue number, linked to its GitHub page after each refresh.
SAVE_CURSOR, RESTORE_CURSOR = "\x1b7", "\x1b8"
ISSUE_NUMBER = re.compile(r"(?<![\w#])#(\d+)(?!\w)")


def color_pairs(colors: int) -> list[tuple[int, int]]:
    """Use the terminal's default text/background with base ANSI accents.

    Pair 2 (divider, working and the legacy yellow) is real yellow: COLOR_YELLOW on
    8 colours, 220 on 256 (r53/r55). The other pairs are the same on both.
    """
    import curses

    return [(-1, -1), (curses.COLOR_YELLOW if colors < 256 else 220, -1),
            (curses.COLOR_GREEN, -1), (-1, -1),
            (curses.COLOR_MAGENTA, -1), (curses.COLOR_GREEN, -1),
            (curses.COLOR_BLUE, -1)]


# xterm-256 index -> base ANSI colour 0..7 for structured art on an 8-colour terminal.
# Fixed once by squared RGB distance (ties to the lower index); never probed per frame.
ANSI8 = (
    0, 1, 2, 3, 4, 5, 6, 7, 7, 1, 2, 3, 4, 5, 6, 7,  # 0..15
    0, 4, 4, 4, 4, 4, 2, 6, 6, 6, 6, 6, 2, 6, 6, 6,  # 16..31
    6, 6, 2, 6, 6, 6, 6, 6, 2, 6, 6, 6, 6, 6, 2, 6,  # 32..47
    6, 6, 6, 6, 1, 5, 5, 5, 5, 5, 3, 3, 5, 5, 5, 7,  # 48..63
    3, 3, 6, 6, 7, 7, 3, 3, 6, 7, 7, 7, 3, 3, 7, 7,  # 64..79
    7, 7, 3, 7, 7, 7, 7, 7, 1, 5, 5, 5, 5, 5, 3, 3,  # 80..95
    5, 5, 7, 7, 3, 3, 7, 7, 7, 7, 3, 3, 7, 7, 7, 7,  # 96..111
    3, 7, 7, 7, 7, 7, 3, 7, 7, 7, 7, 7, 1, 5, 5, 5,  # 112..127
    5, 5, 3, 3, 5, 7, 7, 7, 3, 3, 7, 7, 7, 7, 3, 7,  # 128..143
    7, 7, 7, 7, 3, 7, 7, 7, 7, 7, 3, 7, 7, 7, 7, 7,  # 144..159
    1, 5, 5, 5, 5, 5, 3, 3, 7, 7, 7, 7, 3, 7, 7, 7,  # 160..175
    7, 7, 3, 7, 7, 7, 7, 7, 3, 7, 7, 7, 7, 7, 3, 7,  # 176..191
    7, 7, 7, 7, 1, 5, 5, 5, 5, 5, 3, 7, 7, 7, 7, 7,  # 192..207
    3, 7, 7, 7, 7, 7, 3, 7, 7, 7, 7, 7, 3, 7, 7, 7,  # 208..223
    7, 7, 3, 7, 7, 7, 7, 7, 0, 0, 0, 0, 0, 0, 3, 3,  # 224..239
    3, 3, 3, 3, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,  # 240..255
)


def style_attribute(style: Style, *, styles: dict[str, int], pair: Callable[[int | None, int | None], int]) -> int:
    """The curses attribute of one span's style: named styles by name, ("art", fg, bg) by pair."""
    if isinstance(style, tuple):
        return pair(style[1], style[2])
    return styles.get(style, 0)


class _Palette:
    """Colour pairs beyond the seven base pairs, initialized only when first painted.

    question/next/separator take pairs 8/9/10; structured colours (and working's
    yellow) take cached pairs from 11 up to curses.COLOR_PAIRS. ``inks`` keeps each
    painted style's normalized (fg, bg) for the links written after a refresh.
    """

    def __init__(self, curses, styles: dict[str, int]) -> None:
        self.curses, self.styles = curses, styles
        self.enabled = curses.has_colors()
        self.extended = self.enabled and curses.COLORS >= 256
        self.allocated: dict[tuple[int, int], int] = {}
        self.next = 11
        self.inks: dict[Style, tuple[int, int]] = {}
        dim = curses.A_DIM
        # Named styles: (pair, 256-colour fg, 8-colour fg, attributes on 8 colours or none).
        self.named = {"question": (8, 203, curses.COLOR_RED, 0), "next": (9, 240, curses.COLOR_WHITE, dim),
                      "separator": (10, 242, curses.COLOR_WHITE, dim)}
        if not self.enabled:
            styles.update({name: extra for name, (*_, extra) in self.named.items()})

    def free(self, index: int) -> bool:
        """Whether pair ``index`` exists; COLOR_PAIRS exists once start_color has run."""
        return index < getattr(self.curses, "COLOR_PAIRS", 0)

    def colour(self, value: int | None) -> int:
        if value is None:
            return -1
        return value if self.extended else ANSI8[value]

    def pair(self, foreground: int | None, background: int | None) -> int:
        """A cached pair for two xterm-256 colours; 0 without colours or free pairs."""
        if not self.enabled:
            return 0
        key = self.colour(foreground), self.colour(background)
        if key not in self.allocated:
            if not self.free(self.next):
                return 0
            self.curses.init_pair(self.next, *key)
            self.allocated[key] = self.next
            self.next += 1
        return self.curses.color_pair(self.allocated[key])

    def attribute(self, style: Style) -> int:
        curses = self.curses
        if self.enabled and isinstance(style, str) and style not in self.styles:
            if style == "working":
                self.styles[style] = self.pair(220, None)
                if self.styles[style]:
                    self.inks[style] = self.colour(220), -1
            elif style in self.named:
                index, extended, base, extra = self.named[style]
                ink = extended if self.extended else base
                if self.free(index):
                    curses.init_pair(index, ink, -1)
                    self.styles[style] = curses.color_pair(index) | (0 if self.extended else extra)
                    self.inks[style] = ink, -1
                else:
                    self.styles[style] = extra  # No such pair: the text keeps its dim look.
        attribute = style_attribute(style, styles=self.styles, pair=self.pair)
        if isinstance(style, tuple):
            if self.enabled and attribute:
                self.inks[style] = self.colour(style[1]), self.colour(style[2])
            if style[2] is not None:
                attribute |= curses.A_BOLD  # An acorn body: bold text on its colour.
        return attribute

    def link(self, style: Style, attribute: int) -> str:
        """SGR codes restoring a painted span's look, for a link written over it."""
        curses = self.curses
        codes = [code for flag, code in ((curses.A_BOLD, "1"), (curses.A_DIM, "2"), (curses.A_REVERSE, "7"))
                 if attribute & flag]
        foreground, background = self.inks.get(style, (-1, -1))
        codes += [f"38;5;{foreground}"] * (foreground >= 0) + [f"48;5;{background}"] * (background >= 0)
        return "\x1b[0m" + (f"\x1b[{';'.join(codes)}m" if codes else "")


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


def _place(visible: list, row_map: dict[int, int], footer: int, height: int, alert: list,
           layout: dict | None) -> tuple[list, dict[int, int]]:
    """Put a pinned frame on the screen: its footer rows on the bottom rows, and a refresh
    error in the position row's place above them (or on the last body row when it fits)."""
    if height < 4 or not footer:
        return visible[:height], row_map
    rows = list(visible)
    gap = height - len(rows)
    if gap > 0:
        # A short frame: the footer moves down to the bottom; its hits and rows move with it.
        cut = len(rows) - footer
        rows = rows[:cut] + [[]] * gap + rows[cut:]
        row_map = {y + gap if y >= cut else y: row for y, row in row_map.items()}
        if layout is not None:
            layout["row_map"] = row_map
            layout["hits"] = [(x0, y0 + gap if y0 >= cut else y0, x1, y1 + gap if y0 >= cut else y1, target)
                              for x0, y0, x1, y1, target in layout.get("hits", [])]
    if alert:
        slot = height - footer - 1
        rows[slot] = alert[0]
        row_map = {y: row for y, row in row_map.items() if y != slot}
        if layout is not None:
            layout["row_map"] = row_map
            layout["hits"] = [hit for hit in layout.get("hits", []) if not hit[1] <= slot < hit[3]]
    return rows, row_map


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
                      divider=curses.color_pair(2), yellow=curses.color_pair(2), working=curses.color_pair(2),
                      green=curses.color_pair(3), queue=curses.color_pair(4),
                      hit=curses.color_pair(4) | curses.A_BOLD, claude=curses.color_pair(5),
                      codex=curses.color_pair(6), blue=curses.color_pair(7))
        screen.bkgd(" ", styles["road"])
    palette = _Palette(curses, styles)
    if palette.enabled:
        base = watch.color_pairs(curses.COLORS)
        pairs = {"road": 1, "edge": 1, "divider": 2, "yellow": 2, "working": 2, "green": 3, "queue": 4, "hit": 4,
                 "claude": 5, "codex": 6, "blue": 7}
        palette.inks.update({name: base[index - 1] for name, index in pairs.items()})
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.set_escdelay(25)  # Esc clears the explanation at once.
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
    screen.timeout(500)  # About two frames a second; data keeps its own five-second schedule.
    selected = None
    show_detail = False
    digits = ""
    error = ""
    animation_started = watch.time.monotonic()
    # Data refreshes are due at fixed five-second deadlines from the start; missed ones are
    # skipped, never caught up, and at most one refresh runs at a time.
    next_refresh = animation_started + 5
    refresh_now = False  # A settings save asks for one refresh at once, keeping the deadlines.
    painted = False  # The screen is erased once; later frames paint over it in place.
    pending = None
    started_build = watch._installed_build()
    # Memory across frames: fireworks start times, folds, scroll and the explanation.
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
                if watch.should_reload(started_build, watch._installed_build()):
                    return _restart(cleanup)
            moment = watch.time.monotonic()
            due = moment >= next_refresh
            while next_refresh <= moment:
                next_refresh += 5
            if due or refresh_now:
                if pending is None:
                    pending = worker.submit(refresh)
                    refresh_now = False
                elif due:
                    # A slow snapshot must not hide a reinstall: check once per due refresh.
                    if watch.should_reload(started_build, watch._installed_build()):
                        return _restart(cleanup)
            tick = int((watch.time.monotonic() - animation_started) / 0.5)
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
            row_map = view_state.get("row_map")
            footer = len((view_state.get("pinned_rows") or {}).get("footer", ())) if row_map is not None else 0
            if row_map is not None:
                visible, row_map = _place(visible, row_map, footer, height, alert,
                                          layout if store is not None else None)
                # Keys and the highlight read the state's map: it must name only the
                # rows this screen shows (not a row the error covers).
                view_state["row_map"] = row_map
            elif height >= watch.MIN_SCREEN and len(frame) <= height and visible:
                # F fits: painted as today, its last row (the key help) on the bottom row,
                # where a refresh error takes its place.
                body = visible[:-1]
                visible = body + [[]] * (height - 1 - len(body)) + (alert or visible[-1:])
            elif alert:
                # A refresh error takes the bottom row (in place of the position line).
                bottom = max(0, height - 1)
                visible = visible[:bottom] + [[]] * (bottom - len(visible[:bottom])) + alert
            # One highlighted row, by its position: the first issue row on screen.
            mark = watch.highlighted_row(view_state)
            if mark is None:
                marked = set()
            elif row_map is not None:
                marked = {y for y, row in row_map.items() if row == mark}
            else:
                marked = {mark - top}
            if not painted:
                screen.erase()
                painted = True
            links: list[str] = []
            shown = visible[:max(1, height)]
            for y, line in enumerate(shown):
                x = 0
                # _frame already cut every line to the width; it is painted as it is.
                for value, style in line:
                    attribute = palette.attribute(style) | (curses.A_REVERSE if y in marked else 0)
                    try:
                        screen.addstr(y, x, value, attribute)
                    except curses.error:
                        # Writing the bottom-right cell reports an error after drawing it;
                        # a resize between measuring and painting does too.
                        pass
                    for found in ISSUE_NUMBER.finditer(value):
                        url = watch.issue_url(config, int(found[1]))
                        if url is not None:
                            # The link keeps the span's colours, bold, dim and reverse, then resets.
                            links.append(f"\x1b[{y + 1};{x + watch._cells(value[:found.start()]) + 1}H"
                                         + palette.link(style, attribute) + watch.osc8(url, found[0]) + "\x1b[0m")
                    x += watch._cells(value)
                if x < width:
                    # A screen that only paints (the minimal screen the locked live-view tests
                    # drive) has no move/clrtoeol: it keeps no remnants to clear.
                    try:
                        screen.move(y, x)
                        screen.clrtoeol()  # The rest of a shorter row from the last frame.
                    except (curses.error, AttributeError):
                        pass
            if len(shown) < height:
                try:
                    screen.move(len(shown), 0)
                    screen.clrtobot()  # Rows below the frame that the last frame used.
                except (curses.error, AttributeError):
                    pass
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
            slugs = list(snapshot.status.get("lanes") or ())
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
                    refresh_now = True  # One refresh at once; the five-second deadlines stay.
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
                if keys[key] in ("q", "Q") or (keys[key] == "esc" and store is None):
                    return
                if store is not None or keys[key] == "s":
                    # Esc goes through handle_key: it clears an explanation before it quits.
                    if watch.handle_key(keys[key], store, config, state=view_state, repo_root=repo_root):
                        return
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
