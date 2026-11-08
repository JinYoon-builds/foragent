"""Acceptance for person-name-config. Locked by Foragent; do not edit.

Foragent is open source, so no person's name is built in. An optional ``person_name`` in
.foragent.yml (default: none) sets how the screen addresses the person. The project config
loader is the public entry point the screen reads: a configured name comes out exactly as
written, and a config without it has no name configured. Screen text changes belong to the
#1160 edit, not to this file.
"""

from pathlib import Path

import pytest
import yaml

from foragent.config import ConfigError, load_config

# Names written exactly as a project would; none of them may be altered on the way to the screen.
NAMES = ["진님", "민서 팀장", "Sam Lee"]


def write_project(root: Path, **extra) -> Path:
    settings = {"repo": "owner/repo", "language": "ko", "skill_libraries": [], "notify": "none", **extra}
    (root / ".foragent.yml").write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    return root


def load(root: Path):
    try:
        return load_config(root, global_path=root / "no-global-config.yml")
    except ConfigError as exc:
        raise AssertionError(f"person_name must be an accepted .foragent.yml setting: {exc}") from None


@pytest.mark.parametrize("name", NAMES)
def test_configured_person_name_loads_exactly_as_written(tmp_path, name):
    """Reject ignoring the key, adding or removing an honorific, trimming, or doubling the name."""
    config = load(write_project(tmp_path, person_name=name))
    assert hasattr(config, "person_name"), "The loaded config must expose person_name to the screen"
    assert config.person_name == name, f"The screen must get exactly {name!r}, not {config.person_name!r}"


def test_config_without_person_name_has_no_name_configured(tmp_path):
    """Reject a built-in default name (the maintainer's or any other) when the key is absent."""
    config = load(write_project(tmp_path))
    assert hasattr(config, "person_name"), "The loaded config must expose person_name (none) to the screen"
    assert config.person_name is None, f"Without person_name no name is configured: {config.person_name!r}"


def test_person_name_of_one_project_does_not_leak_into_another(tmp_path):
    """Each project's own config decides; a name loaded earlier is not kept for a project without one."""
    named, plain = tmp_path / "named", tmp_path / "plain"
    named.mkdir()
    plain.mkdir()
    assert load(write_project(named, person_name="민서 팀장")).person_name == "민서 팀장"
    assert load(write_project(plain)).person_name is None


def test_this_repository_configures_its_maintainers_name():
    """Foragent's own .foragent.yml sets person_name 진님, so Jin's screen stays as it is."""
    root = Path(__file__).resolve().parents[2]
    settings = yaml.safe_load((root / ".foragent.yml").read_text(encoding="utf-8"))
    assert settings.get("person_name") == "진님", "This repository configures person_name: 진님"
    try:
        config = load_config(root, global_path=root / "no-global-config.yml")
    except ConfigError as exc:
        raise AssertionError(f"The repository's own settings must load with person_name: {exc}") from None
    assert config.person_name == "진님", config.person_name
