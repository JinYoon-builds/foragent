import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.review_perspectives import DEFAULT_PERSPECTIVES, checklist, perspectives, with_checklist


def test_no_risk_notes_means_no_checklist():
    assert with_checklist("", Config()) == ""


def test_checklist_lists_every_effective_perspective_in_order():
    config = Config(review_perspectives=[{"name": "ops", "focus": "Deploy rollback"}])
    text = with_checklist("NOTES", config)
    assert text.startswith("NOTES\n\n")
    names = [item["name"] for item in perspectives(config)]
    positions = [text.index(f"- [ ] {name}:") for name in names]
    assert positions == sorted(positions) and names[-1] == "ops"
    assert checklist(Config()).count("- [ ]") == len(DEFAULT_PERSPECTIVES) == 7
    assert DEFAULT_PERSPECTIVES[-1]["name"] == "performance"


def test_repeated_project_name_is_rejected(tmp_path):
    item = {"name": "ops", "focus": "x"}
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"review_perspectives": [item, item]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="review_perspectives"):
        load_config(tmp_path)


@pytest.mark.parametrize("item", [{"name": " ", "focus": "x"}, {"name": "a", "focus": 3},
                                  {"name": "a", "focus": "x", "extra": 1}])
def test_bad_item_is_rejected(tmp_path, item):
    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"review_perspectives": [item]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="review_perspectives"):
        load_config(tmp_path)
