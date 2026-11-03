"""Lines for questions routed through the planning session (#970)."""

MESSAGES = {
    "en": {
        "orch_ask_kind_help": "What the answer decides: question (default), merge or order",
        "orch_ask_json_help": "Print the saved question and whether planning is alive as JSON",
        "orch_ask_waiting": "Question {id} ({kind}) saved (open). No planning session is alive: "
                            "it waits in the FA inbox for the next planning session, which asks the person.",
        "orch_ask_planning": "Question {id} ({kind}) saved (open). The planning session is alive: "
                             "send it to planning with SendMessage, then act on the answer recorded "
                             "by `fa orch answer {id}`.",
        "orch_ask_recommended": " (recommended)",
    },
    "ko": {
        "orch_ask_kind_help": "답이 정하는 것: question(기본값), merge 또는 order",
        "orch_ask_json_help": "저장한 질문과 기획 세션이 살아 있는지를 JSON으로 출력해요",
        "orch_ask_waiting": "질문 {id}({kind})를 저장했어요(열림). 살아 있는 기획 세션(planning)이 없어요. "
                            "FA 질문함에 남겨 두면 다음 기획 세션이 사람에게 물어요",
        "orch_ask_planning": "질문 {id}({kind})를 저장했어요(열림). 기획 세션(planning)이 살아 있어요. "
                             "SendMessage로 planning에 보내고, `fa orch answer {id}`로 기록된 답을 보고 움직여요",
        "orch_ask_recommended": " (추천)",
    },
}
