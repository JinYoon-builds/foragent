"""Claude plan share (fa quota mode) and the keep-mode executor switch (#107)."""

MESSAGES = {
    "en": {
        "executor_switch": "#{number} starts with Codex instead of Claude: {reason}.",
        "switch_reason_keep": "Claude keep share {percent}% >= {keep}%",
        "switch_reason_limit": "Claude paused at its limit until {until}",
        "quota_mode_status": "Claude quota mode: {mode} (from {source}); keep stops at {keep_percent}%; "
                             "latest Claude usage: {usage}.",
        "quota_usage_unknown": "unknown",
        "quota_mode_set": "Claude quota mode is now {mode} for this project on this machine. "
                          ".foragent.yml is unchanged.",
        "quota_mode_cleared": "Removed the local Claude quota mode; {mode} from the config applies.",
        "quota_mode_conflict": "Give a mode or --clear, not both.",
    },
    "ko": {
        "executor_switch": "#{number}은 Claude 대신 Codex로 시작해요: {reason}",
        "switch_reason_keep": "Claude 사용량 {percent}%가 keep 기준 {keep}% 이상이에요",
        "switch_reason_limit": "Claude가 {until}까지 사용 한도로 멈췄어요",
        "quota_mode_status": "Claude 사용 방식: {mode}({source}에서 정함). keep은 {keep_percent}%에서 멈춰요. "
                             "최근 Claude 사용량: {usage}",
        "quota_usage_unknown": "모름",
        "quota_mode_set": "이 기계의 이 프로젝트에서 Claude 사용 방식을 {mode}(으)로 바꿨어요. "
                          ".foragent.yml은 그대로예요",
        "quota_mode_cleared": "로컬 Claude 사용 방식을 지웠어요. 설정의 {mode}을(를) 따라요",
        "quota_mode_conflict": "방식과 --clear 중 하나만 주세요",
    },
}
