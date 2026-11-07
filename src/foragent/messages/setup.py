"""Read-only machine setup advice."""

MESSAGES = {
    "en": {
        "setup_no_idle_sleep_fix": (
            "With your consent, a user LaunchAgent running `caffeinate -i -s` can keep "
            "the machine awake between gate runs (no admin needed). "
            "In System Settings > Energy, enable 'Start up automatically after a power failure'."
        ),
    },
    "ko": {
        "setup_no_idle_sleep_fix": (
            "동의한 뒤 사용자 LaunchAgent로 `caffeinate -i -s`를 실행하면 작업 사이에도 잠자기를 막을 수 있어요 "
            "(관리자 권한 불필요). 설치를 제안하며, 지금 설치하거나 실행하지 않아요. "
            "시스템 설정 > 에너지(System Settings > Energy)에서 "
            "'정전 후 자동으로 시동(Start up automatically after a power failure)'도 켜 주세요."
        ),
    },
}
