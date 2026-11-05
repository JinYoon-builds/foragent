"""The Korean Terminal.app font hint names the labels Korean Terminal.app shows (UX r806).

Korean Terminal.app (TTAppPreferences.loctable, macOS 27.2) calls the Text tab's font
control '서체' with a '변경…' button; no Terminal string says '글꼴'. A person following
the hint looks for the words it names, so both forms of the hint use Terminal's labels.
"""

from foragent.i18n import text


def test_korean_font_hint_uses_terminal_app_labels():
    long_hint = text("terminal_hint_font", "ko")
    short_hint = text("terminal_hint_font_short", "ko")
    for hint in (long_hint, short_hint):
        assert "글꼴" not in hint, f"Korean Terminal.app has no '글꼴' label: {hint}"
        assert "서체" in hint, f"Name the Text tab's '서체' control: {hint}"
        assert "D2Coding" in hint and "github.com/naver/d2codingfont" in hint
    assert "변경" in long_hint, f"The long hint names the '변경…' button: {long_hint}"
    for label in ("터미널", "설정", "프로필", "텍스트"):
        assert label in long_hint, f"Keep the path label {label!r}: {long_hint}"
