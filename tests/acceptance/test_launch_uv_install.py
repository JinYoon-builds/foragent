"""Launch A7/C1: the repository's install recipe selects a suitable Python."""

from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[2]
INSTALL = "uv tool install git+https://github.com/JinYoon-builds/foragent"


def test_one_line_uv_recipe_python_note_and_console_script():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12", "Keep the supported Python range"
    assert project["scripts"]["fa"] == "foragent.cli:main"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert INSTALL in readme, "README needs the one-line Git-source uv install used by today's updater"
    assert "fa init" in readme
    paragraphs = re.split(r"\n\s*\n", readme)
    assert any("uv" in p.lower() and "python" in p.lower()
               and re.search(r"fetch\w*|download\w*", p, re.I)
               and re.search(r"suitable|compatible|required|3\.12", p, re.I)
               for p in paragraphs), "Explain in English that uv fetches a suitable Python"
