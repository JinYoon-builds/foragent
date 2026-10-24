"""Version-tolerant loading of persisted dataclass records.

A runner started on older code must keep reading state that a reinstalled,
newer foragent wrote, and the reverse. Unknown fields are ignored on load and
preserved on write-back; missing fields take their dataclass defaults.
User-written configuration (Config) stays strict and does not use this.
"""

from dataclasses import fields
from typing import Any, TypeVar

T = TypeVar("T")


def load_record(cls: type[T], data: dict[str, Any]) -> T:
    """Build cls from a persisted mapping, ignoring fields cls does not know."""
    names = {item.name for item in fields(cls) if item.init}
    return cls(**{key: value for key, value in data.items() if key in names})


def unknown_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """The fields of a persisted mapping that cls does not define."""
    names = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key not in names}


def write_back(cls: type, saved: Any, value: dict[str, Any]) -> dict[str, Any]:
    """value with the unknown fields of the previously saved mapping kept."""
    previous = unknown_fields(cls, saved) if isinstance(saved, dict) else {}
    return {**previous, **value}
