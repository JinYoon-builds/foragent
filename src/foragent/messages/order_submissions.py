"""Retained ordinary order submissions."""

MESSAGES = {
    "en": {
        "order_submission_waiting": "Order {id} waiting for {boundary}.",
        "order_submission_boundary": "Supply a nonempty publication boundary.",
        "order_submission_io": "Could not retain the order submission: {error}",
        "order_submission_defer_help": "Retain the order until this chosen publication boundary",
        "order_submission_urgent_help": "Publish immediately after a person or agent judges this order urgent",
    },
    "ko": {
        "order_submission_waiting": "주문 {id}가 {boundary} 경계를 기다려요 (waiting).",
        "order_submission_boundary": "발행 경계를 빈칸 없이 적어 주세요.",
        "order_submission_io": "주문을 보관하지 못했어요: {error}",
        "order_submission_defer_help": "선택한 발행 경계까지 주문을 보관해요",
        "order_submission_urgent_help": "사람이나 에이전트가 긴급하다고 판단한 주문을 즉시 발행해요",
    },
}
