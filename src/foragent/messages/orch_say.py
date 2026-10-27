"""Short person lines for orchestrator verbs in the view when no --say line was given."""

MESSAGES = {
    "en": {
        "orch_say_retry": "#{issue}: I handed this back for another try.",
        "orch_say_rerun-review": "#{issue}: I reviewed the PR again.",
        "orch_say_resolve": "#{issue}: I decided the review.",
        "orch_say_escalate": "#{issue}: This needs your decision.",
    },
    "ko": {
        "orch_say_retry": "#{issue} 작업을 다시 맡겼어요",
        "orch_say_rerun-review": "#{issue} PR을 다시 리뷰했어요",
        "orch_say_resolve": "#{issue} 리뷰를 판정했어요",
        "orch_say_escalate": "#{issue} 작업에 결정이 필요해요",
    },
}
