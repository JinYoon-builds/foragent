"""Decision linkage view (fa decisions) help and diagnostics."""

MESSAGES = {
    "en": {
        "decisions_help": "Show each decision's linked issues and derived state from local facts",
        "decisions_json_help": "Print the rows as JSON",
        "decisions_ledger_missing": "No decision ledger to read at {path}. This is not an empty ledger.",
        "decisions_none": "The decision ledger has no decisions yet.",
        "decisions_row": "{id}: {state} ({issues})",
        "decisions_no_issues": "no linked issues",
    },
    "ko": {
        "decisions_help": "결정마다 연결된 이슈와 로컬 사실로 계산한 상태를 보여 줘요",
        "decisions_json_help": "행을 JSON으로 출력해요",
        "decisions_ledger_missing": "{path}에 읽을 결정 장부가 없어요. 빈 장부와는 달라요",
        "decisions_none": "결정 장부에 아직 결정이 없어요",
        "decisions_row": "{id}: {state} ({issues})",
        "decisions_no_issues": "연결된 이슈 없음",
    },
}
