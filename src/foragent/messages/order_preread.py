"""`fa order preread` human output about shared machine slots (#992)."""

MESSAGES = {
    "en": {
        "preread_turns_wait": (
            "The machine budget has no free slot now. Each of the {count} issues is read in its own turn, "
            "and each turn waits for a slot by itself, so you may see a wait message more than once."
        ),
    },
    "ko": {
        "preread_turns_wait": (
            "지금 기계 예산에 남은 칸이 없어요. 이슈 {count}개를 한 번에 하나씩 읽고, "
            "차례마다 칸을 따로 기다려요. 그래서 기다림 안내가 여러 번 보일 수 있어요."
        ),
    },
}
