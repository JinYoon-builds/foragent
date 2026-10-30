"""Validate issue path lists and render approved issue bodies."""

from pathlib import Path, PurePosixPath

from foragent.issue import MARKER, acceptance_paths
from foragent.issue_metadata import exclusive_header
from foragent.order_references import body_lines, repository_link
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


def check_destinations(root: Path, files: dict) -> None:
    """Reject published paths that cannot all be written as files, before any write.

    A path may not be another published path's ancestor, an existing directory, or sit
    under an existing non-directory.
    """
    from foragent.order import PlanError

    names = set(files)
    for name in sorted(names):
        ancestors = [str(parent) for parent in PurePosixPath(name).parents if str(parent) != "."]
        clash = next((parent for parent in ancestors if parent in names), None)
        if clash is not None:
            raise PlanError(f"conflicting published paths: {clash} is also a directory of {name}")
        destination = root / name
        if destination.is_symlink() or destination.is_dir():
            raise PlanError(f"published path {name} is an existing directory or symlink")
        blocked = next((parent for parent in ancestors
                        if (root / parent).is_symlink() and not (root / parent).exists()
                        or (root / parent).exists() and not (root / parent).is_dir()), None)
        if blocked is not None:
            raise PlanError(f"published path {name} is under the existing file {blocked}")


def issue_body(item: dict, repo: str, base: str, acceptance_dir: str = "tests/acceptance/",
               references: list[str] = ()) -> str:
    lines = [MARKER, *exclusive_header(item.get("exclusive", [])), "## Goal", item["goal"], "", "## Out of scope", item["out_of_scope"]]
    if item.get("files"):
        lines.extend(["", "## Files", *(f"- {path}" for path in item["files"])])
    lines.extend(body_lines(list(references), repo, base))
    lines.extend(["", "## Done when"])
    for command in item["done_when"]:
        lines.extend(["```sh", command, "```"])
    lines.extend(f"- [ ] {text}" for text in item.get("checklist", []))
    for path in sorted(set(item.get("acceptance_files", {})) | set(acceptance_paths(item["done_when"], acceptance_dir))):
        lines.append(repository_link(path, repo, base))
    return "\n".join(lines) + "\n"

