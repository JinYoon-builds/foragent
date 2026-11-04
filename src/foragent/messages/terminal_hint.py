"""The one-time font hint for Korean views in macOS Terminal.app (#1793).

Two forms, longest first: the view shows the longest one that fits the window whole."""

MESSAGES = {
    "en": {
        "terminal_hint_font": (
            "Hangul gaps? Set Terminal > Settings > Profiles > Text > Font to D2Coding "
            "(github.com/naver/d2codingfont)"),
        "terminal_hint_font_short": "Hangul gaps: Profiles > Text > Font D2Coding, github.com/naver/d2codingfont",
    },
    "ko": {
        "terminal_hint_font": (
            "한글이 벌어져 보이면 터미널 > 설정 > 프로필 > 텍스트 > 글꼴을 "
            "D2Coding으로 바꿔 보세요 (github.com/naver/d2codingfont)"),
        "terminal_hint_font_short": "한글 틈: 설정 > 프로필 > 텍스트 > 글꼴 D2Coding, github.com/naver/d2codingfont",
    },
}
