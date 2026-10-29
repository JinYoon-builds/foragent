"""The settings window (`s`, #173): tabs for the team, me and this computer.

Rows come from foragent.settings (#160): its list, its one-line descriptions and,
through config.set_setting, the writer `fa settings set` uses. No LLM is involved.
While open (``state['settings']``), the window replaces every section between the
header and the help line. It is a package so that section discovery, which stacks
every plain module, skips it: _frame draws it only while it is open. The caller-owned state also holds the tab, the row, a
value changed with ←/→ but not saved yet, a pending team confirmation and one
message line.
"""

from pathlib import Path
from typing import Any

from foragent.config import Config, ConfigError
from foragent.watch_cells import Line
from foragent.watch_data import Snapshot

ORDER = 7  # Where the lane rows were; while open, only the header and help line stand around it.
KEEP = True  # A person opened it; it stays when the screen is short.

TABS = ("team", "me", "computer")
SCOPES = {"team": "team", "me": "me", "computer": "machine"}
# Settings without fixed choices here: `fa settings set` changes them.
READ_ONLY = frozenset({"always_human", "notify", "language", "max_parallel", "log_retention_days"})
# Sections that stay while the window is open: the header and the help line.
FRAMING = ("squirrels", "help")


def is_open(state: dict[str, Any] | None) -> bool:
    return bool(state and state.get("settings"))


def shown_sections(sections: list, state: dict[str, Any] | None) -> list:
    """The sections a frame draws: with the window open, the header, the window and the help line."""
    if not is_open(state):
        return sections
    import sys

    framing = [section for section in sections if section.__name__.rpartition(".")[2] in FRAMING]
    return sorted([*framing, sys.modules[__name__]], key=lambda section: section.ORDER)


def _settings(tab: str) -> list:
    from foragent import settings

    return [setting for setting in settings.SETTINGS if setting.scope == SCOPES[tab]]


def _fresh(config: Config, repo_root: Path | None) -> Config:
    """The configuration as saved now; the given one when the files do not load."""
    from foragent.config import load_config

    if repo_root is None:
        return config
    try:
        return load_config(repo_root)
    except (ConfigError, OSError):
        return config


# The reviewer also cycles through unset (None: the other tool reviews), which no stored choice names.
REVIEWER_CYCLE = ("claude", "codex", None, "none")


def _choices(setting) -> tuple:
    return REVIEWER_CYCLE if setting.key == "reviewer" else setting.choices or ()


def _current(setting, config: Config, store: Any) -> Any:
    from foragent import settings

    return settings.effective_value(setting, config, store)


def _label(setting, value: Any, language: str) -> str:
    from foragent import settings, watch
    from foragent.i18n import MESSAGES

    word = "unset" if value is None else settings.plain(value)
    key = f"settings_window_value_{setting.key}_{word}"
    if key in MESSAGES.get(language, MESSAGES["en"]):
        return watch.text(key, language)
    return settings.plain(value)


def review_label(config: Config) -> str:
    """The review setting's word, as the window and the usage line show it."""
    from foragent import settings

    return _label(settings.BY_KEY["review"], config.review, config.language)


def _value_text(setting, value: Any, store: Any, language: str) -> str:
    from foragent import capacity, watch

    if setting.key == "max_parallel" and value is None:
        # Unset: the machine budget the gate uses now.
        recorded = capacity.read_state(store) if store is not None else None
        count = int(recorded["parallel"]) if recorded else capacity.INITIAL_PARALLEL
        return watch.text("settings_window_auto", language, count=count)
    return _label(setting, value, language)


def _note(setting, store: Any, repo_root: Path | None, language: str) -> str | None:
    """Where a me-tab value comes from when a higher-priority place sets it: the local
    override, else .foragent.yml. Decided by where the key is set, not by comparing values."""
    from foragent import settings, watch

    if setting.scope != "me":
        return None
    if setting.key == "claude_quota.mode" and store is not None and store.claude_quota_mode() is not None:
        return watch.text("settings_window_from_local", language)
    if repo_root is None:
        return None
    try:
        team = settings._yaml_file(settings._team_path(repo_root), language)
        mine = settings._yaml_file(settings.config_module._global_path(), language)
    except ConfigError:
        return None
    if setting.key == "claude_quota.mode":
        quota = team.get("claude_quota")
        found = isinstance(quota, dict) and "mode" in quota
    else:
        found = setting.key in team
    if setting.key == "view_glyphs" and team.get("view_glyphs") == "auto" \
            and mine.get("view_glyphs") in ("emoji", "ascii"):
        found = False  # A repository auto makes no choice; the person's own glyphs apply (load_config).
    return watch.text("settings_window_from_team", language) if found else None


def _tabs(tab: str, config: Config) -> str:
    from foragent import watch

    suffix = "_ascii" if getattr(config, "view_glyphs", "auto") == "ascii" else ""
    names = []
    for name in TABS:
        label = watch.text(f"settings_window_tab_{name}{suffix}", config.language)
        names.append(f"[{label}]" if name == tab else f" {label} ")
    return "  ".join(names)


def _parts(store: Any, config: Config, repo_root: Path | None, tab: str,
           state: dict[str, Any]) -> tuple[list[str], list[list[str]], int, list[str]]:
    """(the tab line, one group of lines per setting, the selected group, the closing lines)."""
    from foragent import watch

    language = config.language
    config = _fresh(config, repo_root)
    rows = _settings(tab)
    selected = min(state.get("settings_row", 0), len(rows) - 1)
    shown = state.get("settings_shown")
    labels = [watch.text(f"settings_window_label_{setting.key}", language) for setting in rows]
    width = max(watch._cells(label) for label in labels)
    groups = []
    for index, (setting, label) in enumerate(zip(rows, labels)):
        value = _current(setting, config, store)
        if shown is not None and shown[0] == setting.key and index == selected:
            value = shown[1]
        text = watch._clean(_value_text(setting, value, store, language))
        if index == selected and setting.key not in READ_ONLY:
            text = f"‹ {text} ›"
        cursor = "›" if index == selected and is_open(state) else " "
        pad = " " * (width - watch._cells(label))
        description = watch.text(f"settings_desc_{setting.key}", language)
        group = [f"{cursor} {label}{pad}  {text}  — {description}"]
        note = _note(setting, store, repo_root, language)
        if note:
            group.append(f"  {' ' * width}  {note}")
        groups.append(group)
    closing = []
    if state.get("settings_prompt") is not None:
        closing.append(watch.text("settings_window_confirm", language))
    elif state.get("settings_message"):
        closing.append(state["settings_message"])
    closing.append(watch.text("settings_window_keys", language))
    return [_tabs(tab, config)], groups, selected, closing


def settings_view(store: Any, config: Config, repo_root: Path | None, tab: str = "team",
                  state: dict[str, Any] | None = None) -> list[str]:
    """The window's text rows for one tab: the tab line, one row per setting with its value
    and #160's one-line description, then the message, the confirmation and the keys."""
    tabs, groups, _, closing = _parts(store, config, repo_root, tab, state or {})
    return [*tabs, "", *(line for group in groups for line in group), "", *closing]


def open_window(state: dict[str, Any]) -> None:
    state.update(settings=True, settings_tab="team", settings_row=0, settings_shown=None,
                 settings_prompt=None, settings_message=None)


def _reset(state: dict[str, Any]) -> None:
    state.update(settings_shown=None, settings_prompt=None, settings_message=None)


def _save(state: dict[str, Any], repo_root: Path | None, key: str, raw: str | None, language: str) -> None:
    """Save through config.set_setting; raw None clears the key (the reviewer's unset)."""
    from foragent import config as config_module
    from foragent import watch

    _reset(state)
    if repo_root is None:
        state["settings_message"] = watch.text("settings_window_no_repo", language)
        return
    try:
        config_module.set_setting(repo_root, key, raw, unset=raw is None, language=language)
    except ConfigError as exc:
        state["settings_message"] = " ".join(str(exc).split())
        return
    state["settings_message"] = watch.text("settings_window_saved", language, name=key)
    state["config_changed"] = True  # The live view reloads its configuration at once.


def handle(key: str, store: Any, config: Config, state: dict[str, Any], repo_root: Path | None,
           confirm: Any = None) -> None:
    """One key while the window is open (q is handled by the caller)."""
    from foragent import settings, watch

    language = config.language
    prompt = state.get("settings_prompt")
    if prompt is not None:
        # The team confirmation: y saves; any other key leaves it unsaved.
        if key in ("y", "Y"):
            _save(state, repo_root, *prompt, language)
            if state.get("config_changed") and store is not None:
                _show_mode(state, store, _fresh(config, repo_root))
        else:
            _reset(state)
        return
    tab = state.get("settings_tab", "team")
    rows = _settings(tab)
    row = min(state.get("settings_row", 0), len(rows) - 1)
    if key in ("esc", "s"):
        state["settings"] = False
        _reset(state)
    elif key in ("tab", "btab"):
        step = 1 if key == "tab" else -1
        state.update(settings_tab=TABS[(TABS.index(tab) + step) % len(TABS)], settings_row=0)
        _reset(state)
    elif key in ("1", "2", "3"):
        state.update(settings_tab=TABS[int(key) - 1], settings_row=0)
        _reset(state)
    elif key in ("j", "down", "k", "up"):
        step = 1 if key in ("j", "down") else -1
        state["settings_row"] = max(0, min(len(rows) - 1, row + step))
        _reset(state)
    elif key in ("left", "right", "enter"):
        setting = rows[row]
        if setting.key in READ_ONLY:
            state["settings_message"] = watch.text("settings_window_read_only", language, name=setting.key)
            return
        current = _current(setting, _fresh(config, repo_root), store)
        shown = state.get("settings_shown")
        value = shown[1] if shown is not None and shown[0] == setting.key else current
        choices = _choices(setting)
        if key != "enter":
            index = choices.index(value) if value in choices else -1
            step = 1 if key == "right" else -1
            state["settings_shown"] = (setting.key, choices[(index + step) % len(choices)])
            state["settings_message"] = None
            return
        if value == current:
            return  # Nothing changed: nothing to save.
        raw = None if value is None else settings.raw(value)
        if setting.scope == "team":
            if confirm is None:
                state["settings_prompt"] = (setting.key, raw)
                return
            if not confirm(watch.text("settings_window_confirm", language)):
                _reset(state)
                return
        _save(state, repo_root, setting.key, raw, language)
        if state.get("config_changed") and store is not None:
            _show_mode(state, store, _fresh(config, repo_root))


def _show_mode(state: dict[str, Any], store: Any, config: Config) -> None:
    """The effective quota mode after a save; the usage line shows it before the next snapshot."""
    from datetime import UTC, datetime

    from foragent import scheduler_quota

    state["quota_mode"] = (scheduler_quota.quota_mode(store, config)[0], datetime.now(UTC))


def _row(row: str, width: int, style: str) -> list[Line]:
    """One window row as it is (aligned), its description wrapped under itself when too wide."""
    from foragent import watch

    if watch._cells(row) <= width:
        return [[(row, style)]]
    head, separator, description = row.partition(" — ")
    indent = watch._cells(head + separator)
    if not separator or indent >= width // 2:
        return watch._wrap(row, width, style)
    parts = watch._wrap(description, width - indent, style)
    return [[(head + separator, style), *parts[0]], *[[(" " * indent, style), *part] for part in parts[1:]]]


def render(snapshot: Snapshot, config: Config, repo_root: Path, **context: Any) -> list[Line]:
    """The window within ``room`` rows (the screen left between the header and the help line).

    The tab line and the closing lines (message or confirmation, then the keys) always show;
    the setting rows around the selected one fill what is left, with one line naming how
    many rows are above and below. Without ``room`` every row shows.
    """
    from foragent import watch

    state = context.get("state")
    if not is_open(state):
        return []
    width, room = context.get("width") or 120, context.get("room")
    tabs, groups, selected, closing = _parts(state.get("store"), config, repo_root,
                                             state.get("settings_tab", "team"), state)
    prompting = state.get("settings_prompt") is not None
    wrap = lambda rows, style="": [line for row in rows for line in _row(row, width, style)]
    head = wrap(tabs, "bold")
    tail = [*wrap(closing[:-1], "yellow" if prompting else ""), *wrap(closing[-1:], "dim")]
    bodies = [wrap(group, "bold" if index == selected else "") for index, group in enumerate(groups)]
    spacer: list[Line] = [[]]
    total = sum(map(len, bodies))
    if room is not None and len(head) + len(tail) + 2 + total > room:
        spacer = []  # Short screen: the blank lines around the rows give way first.
    available = None if room is None else room - len(head) - len(tail) - 2 * len(spacer)
    first = last = selected
    if available is None or total <= available:
        first, last = 0, len(bodies) - 1
    else:
        available -= 1  # One line names the hidden rows.
        used = len(bodies[selected])
        grown = True
        while grown:
            grown = False
            for candidate in (last + 1, first - 1):
                if 0 <= candidate < len(bodies) and used + len(bodies[candidate]) <= available:
                    used += len(bodies[candidate])
                    first, last = min(first, candidate), max(last, candidate)
                    grown = True
    shown = [line for body in bodies[first:last + 1] for line in body]
    if available is not None and len(shown) > available:
        shown = shown[:max(1, available)]  # The selected row alone is too tall: keep its start.
    lines = [*head, *spacer]
    focus = len(lines) + sum(map(len, bodies[first:selected]))
    lines += shown
    if first > 0 or last < len(bodies) - 1:
        lines.append(watch._line(watch.text("settings_window_more", config.language,
                                            above=first, below=len(bodies) - 1 - last), "dim"))
    lines += [*spacer, *tail]
    # From the selected row through the keys: _window keeps these on a screen still too short.
    state["settings_focus_rel"] = (focus, len(lines))
    return lines
