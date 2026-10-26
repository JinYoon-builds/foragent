"""Human-facing text lookup over the per-feature catalogues in foragent.messages.

No strings live here. ``MESSAGES`` is the merged mapping of every discovered
feature module, keyed by language and then message key.
"""

import importlib
import pkgutil

MESSAGES: dict[str, dict[str, str]] = {}


def reload_messages() -> dict[str, dict[str, str]]:
    """Rediscover every foragent.messages module and rebuild MESSAGES in place.

    A (language, key) pair defined by two modules raises ValueError naming the
    key and both modules. On error MESSAGES keeps its previous contents.
    """
    package = importlib.import_module("foragent.messages")
    merged: dict[str, dict[str, str]] = {}
    owners: dict[tuple[str, str], str] = {}
    prefix = package.__name__ + "."
    for info in sorted(pkgutil.iter_modules(package.__path__, prefix), key=lambda item: item.name):
        if info.ispkg:
            continue
        module = importlib.import_module(info.name)
        for language, catalogue in getattr(module, "MESSAGES", {}).items():
            target = merged.setdefault(language, {})
            for key, template in catalogue.items():
                owner = owners.setdefault((language, key), info.name)
                if owner != info.name:
                    raise ValueError(
                        f"message key {key!r} ({language}) is defined in both {owner} and {info.name}"
                    )
                target[key] = template
    MESSAGES.clear()
    MESSAGES.update(merged)
    return MESSAGES


def text(key: str, language: str = "en", **values) -> str:
    language = language.lower().split("-")[0]
    return MESSAGES.get(language, MESSAGES["en"])[key].format(**values)


reload_messages()
