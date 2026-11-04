"""Acceptance test for mechanism-map (#1472). Locked by Foragent; do not edit.

One plain-language map of Foragent's mechanisms: each entry says in plain words what the mechanism
does, where its code is, and which issues and decisions created it. The PM, the architect and the
reviewers read it first; contributors start there from the README. The docs owner rewrites it in place
per merge group. It starts with the merge path as it runs now, with the merge owner on.
"""

from pathlib import Path
import re

from foragent.issue import parse_body
from foragent.run_prompts import build_review_prompt


ROOT = Path(__file__).resolve().parents[2]
DOC = re.compile(r"\bdocs/[\w./-]+\.md\b")
CODE = re.compile(r"\b(?:src|skills)/[\w./*-]+")


def readers():
    """Every first reader of the map, as the text it actually reads."""
    parsed = parse_body("## Goal\nKeep merges safe.\n## Out of scope\nNone.\n"
                        "## Done when\n```sh\ntrue\n```\n")
    return {
        "contributors (README.md)": (ROOT / "README.md").read_text(encoding="utf-8"),
        "PM (skills/fa/SKILL.md)": (ROOT / "skills/fa/SKILL.md").read_text(encoding="utf-8"),
        "architect (skills/fa-architect/SKILL.md)": (ROOT / "skills/fa-architect/SKILL.md").read_text(encoding="utf-8"),
        "docs owner (skills/fa-docs/SKILL.md)": (ROOT / "skills/fa-docs/SKILL.md").read_text(encoding="utf-8"),
        "reviewers (review prompt)": build_review_prompt(parsed, "diff --git a/x b/x"),
    }


def the_map():
    texts = readers()
    shared = set.intersection(*(set(DOC.findall(text)) for text in texts.values()))
    assert len(shared) == 1, (
        f"Exactly one docs file must be named by every first reader {sorted(texts)}; shared: {sorted(shared)}")
    path, = shared
    assert (ROOT / path).is_file(), f"The map {path} must exist"
    return path, texts


def entries(text):
    """Each mechanism entry: a level-2 or level-3 heading with the text up to the next such heading."""
    parts = re.split(r"(?m)^(#{2,3} .+)$", text)
    return [(parts[i].lstrip("#").strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def test_every_first_reader_starts_from_the_one_map():
    path, texts = the_map()
    for reader, text in texts.items():
        if reader.startswith("docs owner"):
            continue  # The docs owner keeps the map; the last case checks how.
        flat = " ".join(text.split())
        at = [m.start() for m in re.finditer(re.escape(path), flat)]
        assert any(re.search(r"\b(?:first|start|begin)\w*", flat[max(0, i - 200):i + 200], re.I) for i in at), (
            f"{reader} must read {path} first, not only mention it")


def test_each_map_entry_names_its_code_issues_and_decisions_and_the_code_exists():
    path, _ = the_map()
    decisions = set(re.findall(r"(?m)^#+ *(D\d+)\b", (ROOT / "docs/decisions.md").read_text(encoding="utf-8")))
    found = entries((ROOT / path).read_text(encoding="utf-8"))
    assert found, f"{path} must hold one entry per mechanism"
    for title, body in found:
        cited = CODE.findall(body)
        assert cited, f"Entry {title!r} must say where its code is"
        for code in cited:
            assert list(ROOT.glob(code.rstrip("."))), f"Entry {title!r} cites {code}, which is not in the repository"
        assert re.search(r"(?<![\w/])#\d+\b", body), f"Entry {title!r} must name the issues that created it"
        named = set(re.findall(r"\bD\d+\b", body))
        assert named <= decisions, (
            f"Entry {title!r} names decisions missing from docs/decisions.md: {sorted(named - decisions)}")
        prose = [line for line in body.splitlines()
                 if len(re.sub(r"`[^`]*`|\S+/\S+", "", line).split()) >= 8]
        assert prose, f"Entry {title!r} must say in plain words what the mechanism does"


def test_the_map_starts_with_the_current_merge_path_through_the_merge_owner():
    path, _ = the_map()
    found = entries((ROOT / path).read_text(encoding="utf-8"))
    title, body = found[0]
    assert re.search(r"merge", title, re.I), f"The map starts with the merge path, not {title!r}"
    assert re.search(r"merge owner", body, re.I), "The merge path entry describes the merge owner, which is on now"
    owners = {str(file.relative_to(ROOT)) for file in (ROOT / "src/foragent").rglob("*.py")
              if "merge" in file.name and "merge_owner" in file.read_text(encoding="utf-8")}
    cited = {str(file.relative_to(ROOT)) for code in CODE.findall(body) for file in ROOT.glob(code.rstrip("."))}
    assert cited & owners, "The merge path entry must point at code that runs the merge owner"


def test_the_docs_owner_rewrites_the_map_in_place_each_merge_group():
    path, texts = the_map()
    skill = " ".join(texts["docs owner (skills/fa-docs/SKILL.md)"].split())
    at = skill.index(path)
    near = skill[max(0, at - 400):at + 400]
    assert re.search(r"(?:rewrit|updat|refresh)\w*.{0,80}in place|in place.{0,80}(?:rewrit|updat|refresh)",
                     near, re.I), "The docs owner rewrites the map in place, not by appending"
    assert re.search(r"merge group", near, re.I), "The docs owner rewrites the map per merge group"
