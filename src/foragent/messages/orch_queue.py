"""`fa orch queue --global`: the cross-lane priority list."""

MESSAGES = {
    "en": {
        "orch_queue_lane_required": "fa orch queue: give a lane name, or --global for the cross-lane list.",
        "orch_queue_global_number": "fa orch queue --global: '{value}' is not an issue number.",
        "orch_queue_global_usage": "fa orch queue --global: give issue numbers or --clear, not both.",
        "orch_queue_global_show": "Cross-lane priority: {order}",
        "orch_queue_global_empty": "No cross-lane priority; ready issues start by file overlap, "
                                   "then lane queue, then number.",
    },
    "ko": {
        "orch_queue_lane_required": "fa orch queue: 레인 이름이나 --global 중 하나를 주세요.",
        "orch_queue_global_number": "fa orch queue --global: '{value}'는 이슈 번호가 아니에요.",
        "orch_queue_global_usage": "fa orch queue --global: 이슈 번호나 --clear 중 하나만 주세요.",
        "orch_queue_global_show": "레인 사이 우선순위: {order}",
        "orch_queue_global_empty": "레인 사이 우선순위가 없어요. 준비된 이슈는 파일 겹침, "
                                   "레인 대기열, 번호 순으로 시작해요.",
    },
}
