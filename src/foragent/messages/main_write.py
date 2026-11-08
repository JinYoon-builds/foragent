"""Lines for retiring a blocked main-write request (q86)."""

MESSAGES = {
    "en": {
        "orch_main_write_help": "Act on the merge owner's main-write requests",
        "orch_main_write_retire_help": "Retire one blocked main-write request so later requests can land",
        "orch_main_write_why_help": "Why the request must not land; kept on the request and shown in status",
        "orch_main_write_by_help": "Who retires the request (default: orchestrator)",
        "orch_main_write_retired": "Retired main write {id} ({title}): {why}. The next owner pass lands the next request.",
    },
    "ko": {
        "orch_main_write_help": "머지 담당의 main 쓰기 요청을 다뤄요",
        "orch_main_write_retire_help": "막힌 main 쓰기 요청 하나를 거둬서 뒤 요청이 들어가게 해요",
        "orch_main_write_why_help": "이 요청이 들어가면 안 되는 이유예요. 요청에 남고 상태에 보여요",
        "orch_main_write_by_help": "요청을 거두는 사람이에요(기본: orchestrator)",
        "orch_main_write_retired": "main 쓰기 {id}({title})를 거뒀어요: {why}. 머지 담당의 다음 차례에 다음 요청이 들어가요",
    },
}
