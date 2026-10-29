"""`fa init`: the one optional starter order offer (#848)."""

MESSAGES = {
    "en": {
        "starter_question": (
            "Starter order: try one tiny task (about 10 minutes) to see the whole flow, "
            "from order to approval to a merged PR? Nothing is created now. [y/N] "),
        "starter_next_step": (
            "Starter order: type /fa and say 'starter: {example}', or name another tiny task "
            "from this project. Foragent plans it, you approve it once, and a lane opens the PR."),
        "starter_example": "add a --version flag",
    },
    "ko": {
        "starter_question": (
            "Starter order: 작은 일 하나로(약 10 minutes) 주문, 승인, PR 병합까지 전체 흐름을 "
            "볼까요? 지금은 아무것도 만들지 않아요. [y/N] "),
        "starter_next_step": (
            "Starter order: /fa를 입력하고 'starter: {example}'라고 말하세요. 이 프로젝트의 다른 "
            "작은 일을 말해도 돼요. Foragent가 계획하고, 한 번 승인하면 레인이 PR을 열어요."),
        "starter_example": "--version 플래그 추가",
    },
}
