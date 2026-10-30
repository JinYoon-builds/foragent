"""Acceptance test for skill-license-fallback. Locked by Foragent; do not edit."""

import json
import subprocess

import pytest


@pytest.fixture
def offline_catalog(tmp_path, monkeypatch, capsys):
    from foragent import cli
    from foragent.config import Config

    library = tmp_path / "knowledge-work-plugins"
    library.mkdir()
    (library / "LICENSE").write_text("SPDX-License-Identifier: Apache-2.0\n", encoding="utf-8")
    config = Config(skill_libraries=[{
        "name": "knowledge-work-plugins", "repo": "offline://knowledge-work-plugins", "path": str(library),
    }])
    monkeypatch.setattr(cli, "load_global_config", lambda: config)

    def no_external_command(*args, **kwargs):
        pytest.fail(f"Catalog acceptance tests must stay offline; unexpected command: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", no_external_command)

    def catalog():
        code = cli.main(["skills", "catalog", "--json"])
        output = capsys.readouterr()
        assert code == 0, f"fa skills catalog --json must succeed: {output.err}"
        payload = json.loads(output.out)
        assert isinstance(payload, list), "The JSON catalog must be a list of skills"
        # #1028 (2026-10-31): Foragent's vendored principles (library "pstack") may also be listed.
        return [item for item in payload if not item["id"].startswith("pstack:")]

    return library, catalog


def write_skill(library, relative, name):
    directory = library / relative
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Offline acceptance fixture for {name}\n---\nUse this skill.\n",
        encoding="utf-8",
    )
    return directory


def test_nested_catalog_skills_inherit_library_root_license(offline_catalog):
    library, catalog = offline_catalog
    expected = {
        "ux-copy": "plugins/product-design/skills/ux-copy",
        "tech-debt": "plugins/engineering/skills/tech-debt",
    }
    for name, relative in expected.items():
        write_skill(library, relative, name)
    skills = catalog()
    assert sorted(item["name"] for item in skills) == sorted(expected), (
        "Catalog must discover both nested skills to report their root LICENSE; "
        f"expected {sorted(expected)}, got {[item['name'] for item in skills]}"
    )
    for item in skills:
        assert item["license"] == "Apache-2.0", (
            f"{item['name']}: inherit Apache-2.0 from the library root LICENSE, not {item['license']!r}"
        )
        assert expected[item["name"]] in item["copies"], f"Catalog must identify the nested copy of {item['name']}"


def test_regression_skill_own_license_overrides_library_root(offline_catalog):
    library, catalog = offline_catalog
    # Use the already-supported shallow layout to isolate license precedence from discovery.
    skill = write_skill(library, "skills/own-license", "own-license")
    (skill / "LICENSE").write_text("SPDX-License-Identifier: MIT\n", encoding="utf-8")
    skills = catalog()
    assert [item["name"] for item in skills] == ["own-license"], "Catalog must include the locally licensed skill"
    assert skills[0]["license"] == "MIT", "A skill's own MIT LICENSE must override the library's Apache-2.0 LICENSE"
