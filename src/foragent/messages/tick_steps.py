"""Gate tick step timing notices (#564)."""

MESSAGES = {
    "en": {
        "tick_step_slow": "Gate tick step {step} took {seconds:.2f} s",
        "tick_step_failed": "Gate tick step {step} failed and the tick went on: {error}",
        "tick_step_deferred": "Gate tick step {step} deferred to a later tick: {error}",
        "tick_refresh_failed": ("GitHub issue refresh failed; this tick re-reads each issue just before"
                                " starting it: {error}"),
        "tick_issue_deferred": "#{number} deferred to a later tick: reading GitHub failed: {error}",
    },
    "ko": {
        "tick_step_slow": "게이트 tick 단계 {step}에 {seconds:.2f}초가 걸렸어요",
        "tick_step_failed": "게이트 tick 단계 {step}가 실패했지만 tick은 계속했어요: {error}",
        "tick_step_deferred": "게이트 tick 단계 {step}를 다음 tick으로 미뤘어요: {error}",
        "tick_refresh_failed": "GitHub 이슈 새로고침이 실패해서, 이번 tick은 시작 직전에 이슈를 하나씩 다시 읽어요: {error}",
        "tick_issue_deferred": "#{number}는 GitHub 읽기가 실패해서 다음 tick으로 미뤘어요: {error}",
    },
}
