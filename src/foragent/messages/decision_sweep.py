"""Decision candidate capture and confirmation."""

MESSAGES = {
    "en": {
        "decision_sweep_install_help": "Install decision capture hooks only with yes; no leaves settings unchanged",
        "decision_sweep_add_help": "Retain a decision candidate after the person confirms it, for batched publication",
        "decision_sweep_candidate_help": "Candidate id from fa order pending --json",
        "decision_sweep_text_help": "The decision confirmed with the person in one line",
        "decision_sweep_invalid": "Choose an existing decision candidate, a one-line confirmed decision and a publication boundary other than decision-sweep.",
        "decision_sweep_settings": "Cannot install decision hooks in {path}: expected a settings object with hook lists.",
        "decision_sweep_failed": "Decision capture is waiting; the saved conversation will be retried at the next hook. See {path}.",
        "decision_sweep_confirmed": "{decision}\n  Quote: {quote}\n  Source: {source}",
        "order_flush_status_confirmed": "confirmed",
    },
    "ko": {
        "decision_sweep_install_help": "yes일 때만 결정 수집 훅을 설치해요. no는 기존 설정을 유지해요",
        "decision_sweep_add_help": "사람에게 확인받은 결정 후보를 묶음 발행할 기록으로 남겨요",
        "decision_sweep_candidate_help": "fa order pending --json에 나온 후보 id",
        "decision_sweep_text_help": "사람에게 한 줄로 확인받은 결정",
        "decision_sweep_invalid": "기존 결정 후보와 확인받은 한 줄 결정, decision-sweep 외의 발행 경계를 골라 주세요",
        "decision_sweep_settings": "{path}에 결정 훅을 설치할 수 없어요. 훅 목록을 담은 설정 객체가 필요해요",
        "decision_sweep_failed": "결정 수집이 대기 중이에요. 보관한 대화는 다음 훅에서 다시 시도해요. {path}에서 확인할 수 있어요",
        "decision_sweep_confirmed": "{decision}\n  인용: {quote}\n  출처: {source}",
        "order_flush_status_confirmed": "확인됨",
    },
}
