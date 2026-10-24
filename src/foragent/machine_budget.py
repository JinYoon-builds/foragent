"""Machine-wide budget settings: one machine.yml for every repository's gate (#365).

The shared budget files (capacity.json, test-slots.json) belong to the machine, so
when ``<data root>/machine.yml`` exists their ceilings and floors come only from it
(built-in defaults fill missing keys). A repository's ``.foragent.yml`` may then only
lower its own admission. Without machine.yml every gate keeps today's behaviour: its
repository values size the shared files.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from foragent.config import ConfigError

if TYPE_CHECKING:
    from foragent.config import Config

MACHINE_FILE = "machine.yml"
# Today's defaults, used for each key machine.yml does not set.
DEFAULTS: dict[str, int | None] = {
    "test_slots": 2, "test_slots_floor": 1, "parallel_floor": 1, "max_parallel": None,
}


def machine_file() -> Path:
    from foragent import capacity

    return capacity.machine_root() / MACHINE_FILE


def _read_file() -> dict[str, Any] | None:
    """The machine.yml mapping, or None when there is no file."""
    path = machine_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: must be a mapping of {', '.join(DEFAULTS)}")
    unknown = sorted(str(key) for key in value if key not in DEFAULTS)
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(unknown)}")
    for key, item in value.items():
        if type(item) is not int or item < 1:
            raise ConfigError(f"{path}: {key} must be a positive integer")
    return value


def sources(config: Config | None = None) -> dict[str, dict[str, Any]]:
    """Each effective machine value and where it came from: machine or default.

    Without machine.yml the default is today's behaviour: ``config``'s own values.
    """
    values = _read_file()
    fallback = dict(DEFAULTS)
    if values is None:
        values = {}
        if config is not None:
            fallback.update(test_slots=config.test_slots, test_slots_floor=config.test_slots_floor,
                            parallel_floor=config.parallel_floor)
    result = {
        key: {"value": values.get(key, default), "source": "machine" if key in values else "default"}
        for key, default in fallback.items()
    }
    path = machine_file()
    if result["test_slots_floor"]["value"] > result["test_slots"]["value"]:
        raise ConfigError(f"{path}: test_slots_floor must be <= test_slots")
    ceiling = result["max_parallel"]["value"]
    if ceiling is not None and result["parallel_floor"]["value"] > ceiling:
        raise ConfigError(f"{path}: parallel_floor must be <= max_parallel")
    return result


def values(config: Config | None = None) -> dict[str, int | None]:
    """The effective machine values, without their sources."""
    return {key: item["value"] for key, item in sources(config).items()}


def configured() -> dict[str, int | None] | None:
    """The machine.yml values with defaults for missing keys, or None without the file."""
    return None if _read_file() is None else values()
