"""Human-facing status vocabulary."""


MESSAGES = {
    "en": {
        "summary": "working {working} · waiting for you {waiting_human} · queued {queued}",
        "working": "working", "blocked": "handled by the orchestrator",
        "waiting-human": "waiting for you", "queued": "queued", "idle": "idle",
        "implementing": "implementing", "checking": "checking", "reviewing": "reviewing",
        "ci": "waiting for CI", "merging": "merging", "resolving-conflict": "resolving conflicts",
        "elapsed": "elapsed {seconds}s", "activity": "last activity {seconds}s ago",
        "waiting_for": "waiting for {issues}",
    },
    "ko": {
        "summary": "일하는 중 {working} · 나를 기다림 {waiting_human} · 순서 대기 {queued}",
        "working": "일하는 중", "blocked": "오케스트레이터가 보는 중",
        "waiting-human": "나를 기다림", "queued": "순서 대기", "idle": "할 일 없음",
        "implementing": "구현", "checking": "확인", "reviewing": "리뷰",
        "ci": "CI 대기", "merging": "머지", "resolving-conflict": "충돌 해결",
        "elapsed": "{seconds}초 경과", "activity": "마지막 움직임 {seconds}초 전",
        "waiting_for": "{issues} 기다림",
    },
}


def text(key: str, language: str = "en", **values) -> str:
    language = language.lower().split("-")[0]
    return MESSAGES.get(language, MESSAGES["en"])[key].format(**values)
