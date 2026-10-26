"""Per-issue review panel size, kept locally under ``<state>/review-reviewers.json``.

`fa orch reviewers <issue> <N>` sets how many independent reviewers check one
issue's risk-map PRs; it wins over `.foragent.yml` `review_reviewers`. The
project config is never written. `--clear` removes the issue's value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from foragent.state import StateStore

if TYPE_CHECKING:
    from foragent.config import Config


def _path(store: StateStore) -> Path:
    return store.root / "review-reviewers.json"


def _load(store: StateStore) -> dict[int, int]:
    path = _path(store)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(n): int(count) for n, count in data.items()}


def _save(store: StateStore, values: dict[int, int]) -> None:
    store._write_json(_path(store), {str(n): values[n] for n in sorted(values)})


def set_count(store: StateStore, n: int, count: int) -> None:
    """Set issue n's reviewer count; a count below 1 raises ValueError and keeps the stored value."""
    if count < 1:
        raise ValueError(count)
    with store._state_lock():
        values = _load(store)
        values[n] = count
        _save(store, values)


def clear(store: StateStore, n: int) -> None:
    with store._state_lock():
        values = _load(store)
        if values.pop(n, None) is not None:
            _save(store, values)


def get(store: StateStore, n: int) -> int | None:
    return _load(store).get(n)


def effective(store: StateStore, config: Config, n: int) -> int:
    """The issue override if one is set, else the project's review_reviewers."""
    override = get(store, n)
    return override if override is not None else int(getattr(config, "review_reviewers", 1) or 1)
