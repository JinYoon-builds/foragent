"""Claude plan share (fa quota mode), executor fallback switches and the credits question (#107, #786)."""

MESSAGES = {
    "en": {
        "executor_switch": "#{number} starts with {target} instead of {source}: {reason}.",
        "switch_reason_keep": "{name} keep share {percent}% >= {keep}%",
        "switch_reason_limit": "{name} paused at its limit until {until}",
        "switch_reason_cap": "{name} spent {spent} of its daily cap {cap}",
        "switch_reason_unmeasured": "{name} reports no cost for its api cap",
        "credits_question": "{name} reached its subscription limit and has no spend_cap. "
                            "Wait for the reset, or allow paid credits up to a daily cap?",
        "credits_wait": "Wait for reset (recommended)",
        "credits_allow": "Allow credits up to a cap",
        "quota_mode_status": "Claude quota mode: {mode} (from {source}); keep stops at {keep_percent}%; "
                             "latest Claude usage: {usage}.",
        "quota_usage_unknown": "unknown",
        "quota_mode_set": "Claude quota mode is now {mode} for this project on this machine. "
                          ".foragent.yml is unchanged.",
        "quota_mode_cleared": "Removed the local Claude quota mode; {mode} from the config applies.",
        "quota_mode_conflict": "Give a mode or --clear, not both.",
    },
    "ko": {
        "executor_switch": "#{number}은 {source} 대신 {target}로 시작해요: {reason}",
        "switch_reason_keep": "{name} 사용량 {percent}%가 keep 기준 {keep}% 이상이에요",
        "switch_reason_limit": "{name}가 {until}까지 사용 한도로 멈췄어요",
        "switch_reason_cap": "{name}가 오늘 하루 상한 {cap} 중 {spent}를 썼어요",
        "switch_reason_unmeasured": "{name}는 비용을 알려 주지 않아 api 상한을 지킬 수 없어요",
        "credits_question": "{name}가 구독 한도에 닿았고 spend_cap이 없어요. "
                            "재설정까지 기다릴까요, 하루 상한까지 유료 크레딧을 쓸까요?",
        "credits_wait": "재설정까지 기다리기(추천)",
        "credits_allow": "상한까지 크레딧 쓰기",
        "quota_mode_status": "Claude 사용 방식: {mode}({source}에서 정함). keep은 {keep_percent}%에서 멈춰요. "
                             "최근 Claude 사용량: {usage}",
        "quota_usage_unknown": "모름",
        "quota_mode_set": "이 기계의 이 프로젝트에서 Claude 사용 방식을 {mode}(으)로 바꿨어요. "
                          ".foragent.yml은 그대로예요",
        "quota_mode_cleared": "로컬 Claude 사용 방식을 지웠어요. 설정의 {mode}을(를) 따라요",
        "quota_mode_conflict": "방식과 --clear 중 하나만 주세요",
    },
}
