"""Review perspectives: a checklist the reviewer works through on risk-map PRs.

Seven product-generic defaults come first, in a fixed order; a project adds its
own from `.foragent.yml` `review_perspectives` ({name, focus} items). A project
item named like a default replaces that default's focus in place; other items
follow the defaults in the order written. The defaults are never mutated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foragent.config import Config

DEFAULT_PERSPECTIVES: tuple[dict[str, str], ...] = (
    {"name": "security",
     "focus": "Spoofing, tampering and the provenance of inputs: who can forge or alter what this code trusts."},
    {"name": "concurrency",
     "focus": "Races, TOCTOU gaps between a check and its use, and stale reads of shared state."},
    {"name": "failure",
     "focus": "Dependencies that are down, slow, or return partial or odd responses."},
    {"name": "compatibility",
     "focus": "Old data, existing installs and old config written before this change."},
    {"name": "realism",
     "focus": "Real environments and real API shapes, not only the fakes the tests use."},
    {"name": "docs",
     "focus": "Behaviour changed, docs unchanged? The lane that changes behaviour updates the related docs "
              "(README, docs/**, skills) in the same PR."},
    {"name": "performance",
     "focus": "Hot paths, repeated work and unbounded growth: extra subprocesses, GitHub calls, file scans "
              "or memory per tick or run."},
)


def perspectives(config: Config) -> list[dict[str, str]]:
    """The effective list: defaults (with project overrides in place), then project additions."""
    effective = [dict(item) for item in DEFAULT_PERSPECTIVES]
    index = {item["name"]: position for position, item in enumerate(effective)}
    for item in getattr(config, "review_perspectives", None) or []:
        name, focus = item["name"], item["focus"]
        if name in index:
            effective[index[name]]["focus"] = focus
        else:
            index[name] = len(effective)
            effective.append({"name": name, "focus": focus})
    return effective


def checklist(config: Config, items: list[dict[str, str]] | None = None) -> str:
    """Reviewer checklist text; covers every perspective unless `items` gives this reviewer's share."""
    lines = ["Review perspectives checklist: check the change from each perspective below "
             "and report findings under any of them."]
    lines += [f"- [ ] {item['name']}: {item['focus']}" for item in (perspectives(config) if items is None else items)]
    return "\n".join(lines)


def with_checklist(notes: str, config: Config, items: list[dict[str, str]] | None = None) -> str:
    """Risk notes followed by the checklist; empty notes (no risk-map path) stay empty."""
    return f"{notes}\n\n{checklist(config, items)}" if notes else ""


def shares(config: Config, count: int) -> list[list[dict[str, str]]]:
    """Split the effective perspectives round-robin in list order: reviewer k gets k, k+N, ...

    N above the number of perspectives is capped at that number.
    """
    items = perspectives(config)
    count = max(1, min(count, len(items)))
    return [items[k::count] for k in range(count)]


def validate(values: dict[str, Any], error: type[Exception]) -> None:
    """Raise `error` with a foragent.messages.review_perspectives text that names the bad key."""
    from foragent.i18n import text

    items = values.get("review_perspectives", [])
    if not isinstance(items, list) or any(
        not isinstance(item, dict) or set(item) != {"name", "focus"}
        or any(not isinstance(item[key], str) or not item[key].strip() for key in ("name", "focus"))
        for item in items
    ):
        raise error(text("review_perspectives_shape"))
    seen: set[str] = set()
    for item in items:
        if item["name"] in seen:
            raise error(text("review_perspectives_repeat", name=item["name"]))
        seen.add(item["name"])
    reviewers = values.get("review_reviewers", 1)
    if type(reviewers) is not int or reviewers < 1:
        raise error(text("review_reviewers_shape"))
