"""Validate issue path lists and render approved issue bodies."""

from pathlib import Path
from urllib.parse import quote

from foragent.issue import MARKER, acceptance_paths
from foragent.issue_metadata import exclusive_header
from foragent.paths import repo_path


def validate_paths(item: dict, root: Path, label: str) -> None:
    from foragent.order import PlanError, _strings

    for key in ("files", "exclusive"):
        for name in _strings(item.get(key, []), f"{label}.{key}"):
            if name.splitlines() != [name]:
                raise PlanError(f"{label}.{key} items must each be a single line")
            try:
                repo_path(root, name)
            except ValueError as exc:
                raise PlanError(f"{label}.{key}: {exc}") from exc


def issue_body(item: dict, repo: str, base: str, acceptance_dir: str = "tests/acceptance/") -> str:
    lines = [MARKER, *exclusive_header(item.get("exclusive", [])), "## Goal", item["goal"], "", "## Out of scope", item["out_of_scope"]]
    if item.get("files"):
        lines.extend(["", "## Files", *(f"- {path}" for path in item["files"])])
    lines.extend(["", "## Done when"])
    for command in item["done_when"]:
        lines.extend(["```sh", command, "```"])
    lines.extend(f"- [ ] {text}" for text in item.get("checklist", []))
    for path in sorted(set(item.get("acceptance_files", {})) | set(acceptance_paths(item["done_when"], acceptance_dir))):
        lines.append(f"[{path}](https://github.com/{repo}/blob/{quote(base, safe='')}/{quote(path)})")
    return "\n".join(lines) + "\n"

