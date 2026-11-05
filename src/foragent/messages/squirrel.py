"""Full phase words and their acorn abbreviations, in one vocabulary."""

# Short forms are keyed by the approved full word, never by a second phase vocabulary.
SHORT_WORDS = {
    "squirrel_code": {"en": "write", "ko": "작성"},
    "squirrel_baseline": {"en": "prep", "ko": "준비"},
    "squirrel_checking": {"en": "goal", "ko": "목표"},
    "squirrel_reviewing": {"en": "review", "ko": "리뷰"},
    "squirrel_ci": {"en": "full", "ko": "전체"},
    "squirrel_merging": {"en": "merge", "ko": "합침"},
    "squirrel_turn": {"en": "turn", "ko": "차례"},
}
PHASE_WORDS = {
    "baseline": "squirrel_baseline", "implementing": "squirrel_code", "resolving": "squirrel_code",
    "checking": "squirrel_checking", "reviewing": "squirrel_reviewing", "ci": "squirrel_ci",
    "merging": "squirrel_merging", "merge-lock-wait": "squirrel_merging",
    "resolving-conflict": "squirrel_merging", "merge-owner-wait": "squirrel_turn",
}


def phase_word(phase: str | None, language: str, *, short: bool = False) -> str:
    from foragent.i18n import text

    key = PHASE_WORDS.get(phase, "squirrel_turn")
    return text(key + ("_short" if short else ""), language)

MESSAGES = {
    "en": {
        "squirrel_code": "writing code",
        "squirrel_baseline": "measuring the baseline",
        "squirrel_checking": "goal tests",
        "squirrel_reviewing": "in review",
        "squirrel_ci": "full tests",
        "squirrel_merging": "testing merged",
        "squirrel_turn": "waiting for a turn",
        "squirrel_person": "waiting for a person",
        "squirrel_orchestrator": "waiting for the orchestrator",
        "squirrel_minutes": "{minutes}m",
        "squirrel_hours": "{hours}h {minutes}m",
        "squirrel_header": "today ✓{done} · working {working} · waiting {waiting} · queued {queued}",
        "squirrel_footer": "q: quit · s · m · j/k",
    },
    "ko": {
        "squirrel_code": "코드 쓰는 중",
        "squirrel_baseline": "기준 재는 중",
        "squirrel_checking": "목표 시험 중",
        "squirrel_reviewing": "리뷰 받는 중",
        "squirrel_ci": "전체 시험 중",
        "squirrel_merging": "합쳐서 시험 중",
        "squirrel_turn": "차례 기다리는 중",
        "squirrel_person": "사람 기다리는 중",
        "squirrel_orchestrator": "오케스트레이터 기다리는 중",
        "squirrel_minutes": "{minutes}분",
        "squirrel_hours": "{hours}시간 {minutes}분",
        "squirrel_header": "오늘 ✓{done} · 일하는 중 {working} · 기다리는 일 {waiting} · 차례 {queued}",
        "squirrel_footer": "q: 종료 · s · m · j/k",
    },
}

for _key, _words in SHORT_WORDS.items():
    for _language, _word in _words.items():
        MESSAGES[_language][_key + "_short"] = _word
