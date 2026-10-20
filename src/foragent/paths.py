"""Repository-relative paths used by deterministic authoring tools."""

from pathlib import Path, PurePosixPath


class PathError(ValueError):
    """An authoring destination is not a repository-relative path."""


def repo_path(root: Path, name: str) -> Path:
    if (not isinstance(name, str) or not name or "\\" in name or "\0" in name
            or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
            or ".git" in PurePosixPath(name).parts
            or PurePosixPath(name).as_posix() in {".", ""}):
        raise PathError(f"expected a repository-relative path: {name!r}")
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise PathError(f"path escapes repository: {name}")
    return path
