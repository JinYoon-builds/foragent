"""Shared helper for the not-set-up next-step acceptance tests (UX r1108).

A git repository with no .foragent.yml, the person's language in the personal
config (~/.config/foragent/config.yml under the test's isolated HOME), and a
runner that calls the public `fa` entry point in process.
"""

from pathlib import Path
import re
import subprocess

import yaml

COMMANDS = {
    "dashboard --once": (["dashboard", "--once"], 1),
    "dashboard --open": (["dashboard", "--open"], 1),
    "brief": (["brief"], 0),
}

# "/fa" as its own command: not "/fa-order", not a path like "src/fa"; a Korean
# particle may follow it ("/fa를").
SLASH_FA = re.compile(r"(?<![A-Za-z0-9_/])/fa(?![A-Za-z0-9_/-])")
HANGUL = re.compile(r"[가-힣]")


def plain_repo(tmp_path: Path, language: str | None) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert not (root / ".foragent.yml").exists()
    if language is not None:
        personal = Path.home() / ".config/foragent/config.yml"
        personal.parent.mkdir(parents=True, exist_ok=True)
        personal.write_text(yaml.safe_dump({"language": language}))
    return root


def run(argv, root: Path, capsys):
    from foragent.cli import main

    capsys.readouterr()
    try:
        code = main([*argv, "--repo-root", str(root)])
    except SystemExit as stop:  # argparse or an explicit exit
        code = stop.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def message_lines(out: str, err: str) -> list[str]:
    """Every printed line, trimmed, without the CLI's 'foragent: ' error prefix."""
    lines = []
    for line in (out + "\n" + err).splitlines():
        line = line.strip()
        if line.startswith("foragent:"):
            line = line[len("foragent:"):].strip()
        if line:
            lines.append(line)
    return lines
