"""Locked S2 acceptance: retired review values fail at the configuration boundary."""

import pytest
import yaml

from foragent.config import ConfigError, load_config


@pytest.mark.parametrize("scope", ["project", "global"])
@pytest.mark.parametrize("value", [{"review": "batch"}, {"review_paths": {"src/**": "batch"}}],
                         ids=["review", "path-override"])
def test_retired_batch_value_names_the_remaining_choices(tmp_path, scope, value):
    """Reject accepting batch, or rejecting it with a message that hides valid choices."""
    project, global_path = tmp_path / ".foragent.yml", tmp_path / "global.yml"
    project.write_text("{}\n", encoding="utf-8")
    path = project if scope == "project" else global_path
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(tmp_path, global_path)
    message = str(caught.value)
    assert "fast" in message and "careful" in message, message
    if "review" in value:
        assert message == "review must be fast or careful"
    else:
        assert "review_paths" in message and "src/**" in message, message


def test_default_review_is_a_supported_choice(tmp_path):
    """Reject retaining batch as the implicit default after removing it from valid values."""
    (tmp_path / ".foragent.yml").write_text("{}\n", encoding="utf-8")
    config = load_config(tmp_path, tmp_path / "missing-global.yml")
    assert config.review == "fast"
    assert config.checks == "local"
