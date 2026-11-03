"""Review perspectives on risk-map PRs: each one gets its own whole-change reviewer.

Six core defaults (#1651) come first, in a fixed order; a project adds its own from
`.foragent.yml` `review_perspectives` ({name, focus} items). A project item
named like a default replaces that default's focus in place; other items follow
the defaults in the order written. The defaults are never mutated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foragent.config import Config

DEFAULT_PERSPECTIVES: tuple[dict[str, str], ...] = (
    # The code-review plugin's five reviewers plus fatal-path (#1651); skills/review-procedure
    # holds the procedure, this tuple only the names and one-line focus each reviewer gets.
    {"name": "rule-documents",
     "focus": "Does the change comply with each rule and instruction in the project's rule documents "
              "(CLAUDE.md, AGENTS.md, docs/rules.md) and the issue's Goal and Out of scope?"},
    {"name": "obvious-bugs",
     "focus": "A shallow scan of the changed lines for obvious, large bugs; no nitpicks and no likely "
              "false positives."},
    {"name": "git-history",
     "focus": "Read the git blame and history of the modified code: is there a bug in light of that history?"},
    {"name": "earlier-pr-comments",
     "focus": "Read the comments on earlier PR reviews that touched these files: do they also apply here?"},
    {"name": "code-comments",
     "focus": "Read the code comments in the modified files: does the change comply with their guidance?"},
    {"name": "fatal-path",
     "focus": "A path, however rare, that loses work or data, puts wrong code on main, merges without "
              "approval, hands another person's permission to an agent, or makes the gate stall: name "
              "the input that reaches it."},
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
    """Reviewer checklist text; covers every perspective unless `items` gives this reviewer's own."""
    lines = ["Review perspectives checklist: check the change from each perspective below "
             "and report findings under any of them."]
    lines += [f"- [ ] {item['name']}: {item['focus']}" for item in (perspectives(config) if items is None else items)]
    return "\n".join(lines)


def with_checklist(notes: str, config: Config, items: list[dict[str, str]] | None = None) -> str:
    """Risk notes followed by the checklist; empty notes (no risk-map path) stay empty."""
    return f"{notes}\n\n{checklist(config, items)}" if notes else ""


def shares(config: Config) -> list[list[dict[str, str]]]:
    """One whole-change reviewer per effective perspective, in list order.

    The reviewer count never merges perspectives into one reviewer; it only limits how
    many reviewers run at once.
    """
    return [[item] for item in perspectives(config)]


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
