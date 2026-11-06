"""`fa init` default skills: a bare name several skills share is picked, not refused (#2243, q24).

<lane> is a placeholder: default skills belong to roles, not to one lane.
"""

MESSAGES = {
    "en": {
        "init_skill_picked": (
            "{name} matches several skills ({others}); setup uses {picked}. "
            "To use another one: fa lanes skills <lane> <id>, for example fa lanes skills <lane> {picked}"
        ),
    },
    "ko": {
        "init_skill_picked": (
            "{name} 이름의 스킬이 여러 개예요({others}). 설정은 {picked}를 써요. "
            "다른 스킬을 쓰려면: fa lanes skills <lane> <id>, 예: fa lanes skills <lane> {picked}"
        ),
    },
}
