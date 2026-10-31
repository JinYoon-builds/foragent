"""Approved issues whose locked tests an order would change (#1107)."""

MESSAGES = {
    "en": {
        "order_locks_heading": "Approved issues whose locked tests this order changes:",
        "order_locks_refused": (
            "Nothing was published: this order changes locked tests of approved issues outside it, "
            "and the next gate check would revoke their approval.\n{owners}\n"
            "To go ahead, withdraw those approvals, publish, then approve them again."),
    },
    "ko": {
        "order_locks_heading": "이 주문이 잠긴 시험을 바꾸는 승인된 이슈:",
        "order_locks_refused": (
            "아무것도 발행하지 않았어요. 이 주문이 주문 밖 승인된 이슈의 잠긴 시험을 바꿔서, "
            "다음 게이트 검사에서 그 승인이 풀려요.\n{owners}\n"
            "진행하려면 그 승인을 내리고, 발행한 뒤, 다시 승인해 주세요."),
    },
}
