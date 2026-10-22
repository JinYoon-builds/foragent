"""Acceptance test for voice. Locked by Foragent; do not edit."""

from pathlib import Path
import re

import pytest


def test_voice_document_defines_korean_and_english_rules():
    path = Path(__file__).resolve().parents[2] / "docs/voice.md"
    assert path.is_file(), "docs/voice.md must define the shared voice"
    text = " ".join(path.read_text(encoding="utf-8").casefold().split())
    for variants in (("해요체",), ("active voice", "능동태"), ("positive phrasing", "긍정"),
                     ("noun stacking", "명사 나열", "명사를 쌓"), ("conversational",), ("contractions",)):
        assert any(word in text for word in variants), variants
    assert re.search(r"(?:no|avoid).{0,40}noun stacking|명사.{0,40}(?:금지|피하|쌓지|나열하지)", text)
    assert re.search(r"contractions.{0,40}(?:allowed|welcome|fine)|(?:allow|use).{0,40}contractions", text)


def test_voice_translations_avoid_formal_korean_and_stiff_english():
    from foragent.i18n import MESSAGES

    bad = {key: value for key, value in MESSAGES["ko"].items()
           if any(word in value for word in ("니다", "하십시오", "께서", "여쭈"))}
    assert bad == {}, f"Korean messages must use 해요체: {bad}"
    assert not {key: value for key, value in MESSAGES["en"].items()
                if any(word in value.casefold() for word in ("please kindly", "be advised"))}


@pytest.mark.parametrize("key,values", [
    ("watch_all_idle", {}), ("watch_open_failed", {"error": "offline"}),
    ("watch_terminal", {}),
])
def test_voice_known_sentence_keys_render_with_haeyo_ending(key, values):
    from foragent.i18n import text

    message = text(key, "ko", **values)
    assert message.endswith(("요", "요.")), f"{key}: {message}"
