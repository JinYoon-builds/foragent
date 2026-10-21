"""Record source provenance when setuptools builds or installs the package."""

from pathlib import Path
import subprocess


def write_build_metadata(root: Path) -> None:
    target = root / "src" / "foragent" / "_build.py"
    # An sdist already contains the original build's provenance. Do not use
    # an unrelated parent repository when rebuilding the extracted archive.
    if not (root / ".git").exists() and target.exists():
        return
    sha = "unknown"
    if (root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"], cwd=root,
                check=True, capture_output=True, text=True, timeout=5,
            )
            sha = result.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            pass
    target.write_text(
        f'"""Generated at build/install time; do not edit."""\n\nGIT_SHA = {sha!r}\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    from setuptools import setup

    write_build_metadata(Path(__file__).resolve().parent)
    setup()
