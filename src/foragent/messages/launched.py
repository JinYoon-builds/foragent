"""The refusal a session Foragent launched gets for a person-only command (#1736)."""

MESSAGES = {
    "en": {
        "launched_person_only": ("fa {command} is for a person. Foragent started this session, "
                                 "so nothing changed."),
        "free_text_stdin_once": "{options}: only one option per command can read stdin (-). Nothing changed.",
    },
    "ko": {
        "launched_person_only": ("fa {command}: 사람이 실행하는 명령이에요. Foragent가 시작한 세션이라 "
                                 "아무것도 바꾸지 않았어요."),
        "free_text_stdin_once": "{options}: 표준 입력(-)은 명령 하나에 옵션 하나만 읽을 수 있어요. 아무것도 바꾸지 않았어요.",
    },
}
