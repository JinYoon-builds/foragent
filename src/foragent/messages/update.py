"""Release check and `fa update` lines."""

MESSAGES = {
    "en": {
        "update_offer": "Foragent {latest} is out (you have {current}). Run: {command}",
        "update_lookup_failed": "Couldn't look up the latest Foragent release. Nothing changed; try again later.",
        "update_none": "Foragent {current} is the latest release.",
        "update_installed": "Installed Foragent {tag}.",
        "update_install_failed": "Couldn't install Foragent {tag}: {reason}. No gate was restarted.",
        "update_no_gates": "No installed gates to restart.",
        "update_gate_restarted": "Restarted the gate for {repo}.",
        "update_gate_busy": "Skipped {repo} ({root}): a tick is running. Run fa update again after it ends.",
        "update_gate_not_loaded": "Skipped {root}: its gate isn't loaded.",
        "update_gate_failed": "Skipped {root}: {reason}",
        "update_record_failed": "Couldn't record the update result: {reason}",
    },
    "ko": {
        "update_offer": "Foragent {latest}이 나왔어요(지금 {current}). 실행: {command}",
        "update_lookup_failed": "최신 Foragent 릴리스를 확인하지 못했어요. 바뀐 것은 없어요. 나중에 다시 해 보세요.",
        "update_none": "Foragent {current}이 최신 릴리스예요.",
        "update_installed": "Foragent {tag}을 설치했어요.",
        "update_install_failed": "Foragent {tag}을 설치하지 못했어요: {reason}. 게이트는 다시 시작하지 않았어요.",
        "update_no_gates": "다시 시작할 설치된 게이트가 없어요.",
        "update_gate_restarted": "{repo} 게이트를 다시 시작했어요.",
        "update_gate_busy": "{repo}({root})는 건너뛰었어요: tick이 실행 중이에요. 끝난 뒤 fa update를 다시 실행하세요.",
        "update_gate_not_loaded": "{root}는 건너뛰었어요: 게이트가 로드되어 있지 않아요.",
        "update_gate_failed": "{root}는 건너뛰었어요: {reason}",
        "update_record_failed": "업데이트 결과를 기록하지 못했어요: {reason}",
    },
}
