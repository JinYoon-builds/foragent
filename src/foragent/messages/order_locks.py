"""Shared locked acceptance files across approved issues (#1107)."""

MESSAGES = {
    "en": {
        "order_locks_heading": "Approved issues whose locked tests this order changes:",
        "order_locks_refused": (
            "Nothing was published: this order changes locked tests of approved issues, "
            "and not all of them are approved again in this publication, so their approval would be revoked.\n"
            "{owners}\n"
            "Approve every listed issue in this publication (add issues from other orders as edits), "
            "or keep the test bytes unchanged."),
        "order_locks_approval_unordered": (
            "Published, but #{number} could not be approved after its last edit, so it is not approved now. "
            "Approve it again in a moment."),
    },
    "ko": {
        "order_locks_heading": "이 주문이 잠긴 시험을 바꾸는 승인된 이슈:",
        "order_locks_refused": (
            "아무것도 발행하지 않았어요. 이 주문이 승인된 이슈의 잠긴 시험을 바꾸는데, "
            "이번 발행에서 그 이슈를 모두 다시 승인하지 않아서 승인이 풀려요.\n{owners}\n"
            "목록의 이슈를 이번 발행에서 모두 승인하거나(다른 주문의 이슈는 edits로 넣어요), "
            "시험 내용을 그대로 두세요."),
        "order_locks_approval_unordered": (
            "발행은 됐지만 #{number}을 마지막 수정보다 뒤에 승인하지 못해서 지금은 승인되지 않았어요. "
            "잠시 뒤 다시 승인해 주세요."),
    },
}
