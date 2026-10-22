"""Acceptance test for settings-ask. Locked by Foragent; do not edit."""

from pathlib import Path
import re


def settings_skill(name):
    return " ".join((Path(__file__).resolve().parents[2] / f"skills/{name}/SKILL.md").read_text().lower().split())


def test_settings_ask_first_use_asks_only_merge_and_quota_with_batch_recommended():
    text = settings_skill("fa-init")
    assert "fa settings set" in text, "First-use answers must use the same settings writer as the UI"
    assert re.search(r"(?:ask|question).{0,160}(?:only|just).{0,160}(?:review|merge).{0,240}(?:quota|claude_quota)", text)
    assert re.search(r"batch.{0,35}(?:recommend|추천)|(?:recommend|추천).{0,35}batch", text)
    for choice in ("fast", "batch", "careful", "keep", "burn"):
        assert re.search(rf"\b{choice}\b", text), choice
    assert not re.search(r"(?:ask|question).{0,100}(?:review mode and executor|choose (?:an? )?executor)", text)


def test_settings_ask_natural_language_changes_use_command_and_disclose_team_scope():
    text = settings_skill("fa-orchestrator")
    assert "fa settings set" in text
    assert "자동 머지 꺼 줘" in text or "turn off automatic merge" in text
    assert re.search(r"(?:report|summari[sz]e|confirm).{0,100}(?:one.line|single.line)", text)
    assert re.search(r"(?:team|everyone|all (?:users|members)).{0,160}(?:appl|affect)|(?:appl|affect).{0,160}(?:team|everyone|all (?:users|members))", text)


def test_settings_ask_skills_offer_commands_instead_of_manual_config_editing():
    for name in ("fa-init", "fa-orchestrator"):
        text = settings_skill(name)
        # A writer must exist in the instructions; mere absence of manual advice is insufficient.
        assert "fa settings set" in text, name
        assert not re.search(r"(?<!never )(?<!not )(?<!don't )(?:ask|tell|instruct) (?:the )?(?:user|person|human).{0,100}(?:edit|change|modify).{0,80}\.foragent\.yml", text), name
        assert not re.search(r"(?:you (?:can|should|must)|manually).{0,60}(?:edit|modify).{0,80}\.foragent\.yml", text), name
