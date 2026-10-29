"""Machine-wide budget settings: one machine.yml for every repository's gate (#365).

The shared budget files (capacity.json, test-slots.json) belong to the machine, so
their ceilings come only from ``<data root>/machine.yml``; the Config
class defaults fill every key the file does not set validly. A repository's
``.foragent.yml`` never sizes the shared files; it may only lower its own admission.

The file is read on every call (no cache), and a bad file never raises: each
invalid key falls back to its default and is reported with source ``invalid``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from foragent.config import IGNORED_KEYS, Config

MACHINE_FILE = "machine.yml"
KEYS = ("test_slots", "max_parallel")
DEFAULTS: dict[str, int | None] = {item.name: item.default for item in fields(Config) if item.name in KEYS}


@dataclass(frozen=True)
class MachineBudget:
    values: dict[str, int | None]
    sources: dict[str, str]  # machine, settings, default or invalid
    unknown_keys: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        """The status JSON shape: {key: {value, source}}, plus unknown_keys when present."""
        result: dict[str, Any] = {
            key: {"value": self.values[key], "source": self.sources[key]} for key in KEYS
        }
        if self.unknown_keys:
            result["unknown_keys"] = self.unknown_keys
        return result


def machine_file() -> Path:
    from foragent import capacity

    return capacity.machine_root() / MACHINE_FILE


def _valid(value: Any) -> bool:
    # YAML booleans are ints in Python; they, null, strings and floats are invalid.
    return type(value) is int and value >= 1


def read() -> MachineBudget:
    """Read machine.yml now, then let `fa settings`' machine max_parallel win (source ``settings``)."""
    from foragent import settings

    budget = _read_file()
    cap = settings.read_machine().get("max_parallel")
    if cap is None:
        return budget
    return MachineBudget({**budget.values, "max_parallel": cap},
                         {**budget.sources, "max_parallel": "settings"}, budget.unknown_keys)


def _read_file() -> MachineBudget:
    """Read machine.yml now; invalid keys use defaults, unknown keys are ignored."""
    values, sources = dict(DEFAULTS), dict.fromkeys(KEYS, "default")
    try:
        data = yaml.safe_load(machine_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return MachineBudget(values, sources)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        data = False  # Unreadable: every key is invalid.
    # Not a mapping (a top-level null or an empty file included): every key is invalid.
    if not isinstance(data, dict):
        return MachineBudget(values, dict.fromkeys(KEYS, "invalid"))
    for key in KEYS:
        if key in data:
            if _valid(data[key]):
                values[key], sources[key] = data[key], "machine"
            else:
                sources[key] = "invalid"
    # Retired floor keys (#497) are accepted and ignored, never reported as unknown.
    unknown = sorted(str(key) for key in data if key not in KEYS and key not in IGNORED_KEYS)
    return MachineBudget(values, sources, unknown)


def values() -> dict[str, int | None]:
    """The effective machine values for the shared budget files."""
    return read().values
