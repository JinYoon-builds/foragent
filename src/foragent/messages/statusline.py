"""fa statusline --uninstall and fa init statusline notices."""

MESSAGES = {
    "en": {
        "statusline_restored": "Restored your previous Claude statusline: {command}",
        "statusline_removed": "Removed the Foragent statusline. You had no statusline before, so none is set now.",
        "statusline_not_installed": "The Foragent statusline is not installed, so there is nothing to uninstall.",
        "statusline_user_changed": ("Left {path} unchanged: the user changed or removed statusLine after "
                                    "Foragent installed it."),
        "statusline_settings_missing": "Could not uninstall: {path} is missing (not found).",
        "statusline_settings_unreadable": "Could not uninstall: could not read {path} ({error}).",
        "statusline_hook_removed": "Removed the Foragent session-start hook and prompt hook from Claude settings.",
        "statusline_hook_added": ("Claude statusline: added the session-start summary and the planning-turn box facts "
                                  "(presence.session_start: false turns them off)."),
    },
    "ko": {
        "statusline_restored": "예전 Claude 상태 줄로 되돌렸어요: {command}",
        "statusline_removed": "Foragent 상태 줄을 뺐어요. 예전에 쓰던 상태 줄이 없어서 지금은 비어 있어요",
        "statusline_not_installed": "Foragent 상태 줄이 설치돼 있지 않아서 뺄 게 없어요",
        "statusline_user_changed": "설치 뒤 사용자가 statusLine을 바꾸거나 지워서 {path}는 그대로 뒀어요",
        "statusline_settings_missing": "{path} 파일이 없어서 상태 줄을 뺄 수 없어요",
        "statusline_settings_unreadable": "{path} 파일을 읽지 못해서 상태 줄을 뺄 수 없어요({error})",
        "statusline_hook_removed": "Claude 설정에서 Foragent 세션 시작 훅과 프롬프트 훅을 뺐어요",
        "statusline_hook_added": "Claude 상태 줄: 세션 시작 요약과 기획 차례마다 받을 상자 소식을 더했어요(presence.session_start: false로 끌 수 있어요)",
    },
}
