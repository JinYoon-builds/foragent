"""Foragent presence in Claude Code: the statusline segment (#754) and the session-start line (#755)."""

MESSAGES = {
    "en": {
        "presence_statusline": "🐿 {lanes} lanes working · ✋ {turns} · {merged} merged today",
        "presence_session_start": "since the last report: {merged} merges · ✋ {turns} · /fa for the report",
    },
    "ko": {
        "presence_statusline": "🐿 레인 {lanes} 일하는 중 · ✋ {turns} · 오늘 머지 {merged}",
        "presence_session_start": "지난 보고 이후 머지 {merged} · ✋ {turns} · /fa 로 보고 보기",
    },
}
