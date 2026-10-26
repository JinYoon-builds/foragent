"""Lines for `fa lanes talk` and the tick that ends a talk (D55 addendum)."""

MESSAGES = {
    "en": {
        "talk_no_session": "#{n} has no recorded session to talk to yet.",
        "talk_log_opened": "Opened the live log of #{n} ({screen}); stall checks pause while it stays open.",
        "talk_reshown": "Showed the open talk terminal of #{n} ({screen}).",
        "talk_resume_opened": "Opened the session of #{n} ({screen}).",
        "talk_sent": "Sent your message to the running worker of #{n}; its live log is open.",
        "talk_hint_saved": "Saved your message for the next attempt of #{n}; its live log is open.",
        "talk_queue_failed": "Could not send your message to #{n}: {error}. The live log stays open.",
        "talk_no_handle": "Terminal gives no handle, so stall checks keep running.",
        "talk_manual": "No terminal opener found; run: {command}",
        "talk_ended": "#{n} talk terminal closed; stall clock restarted",
        "talk_run_changed": "#{n} started a new run while the terminal opened; run fa lanes talk {n} again.",
        "talk_session_starting": "#{n} is starting a new worker session; send the message again in a minute.",
        "talk_record_unreadable": "Cannot read the talk record of #{n} ({path}); close its terminal, delete the file, then retry.",
    },
    "ko": {
        "talk_no_session": "#{n}에는 아직 대화할 세션 기록이 없어요.",
        "talk_log_opened": "#{n}의 실시간 로그를 열었어요 ({screen}). 열려 있는 동안 정지 검사를 멈춰요.",
        "talk_reshown": "열려 있는 #{n}의 대화 터미널을 다시 보여 줬어요 ({screen}).",
        "talk_resume_opened": "#{n}의 세션을 열었어요 ({screen}).",
        "talk_sent": "실행 중인 #{n} 작업자에게 메시지를 보냈어요. 실시간 로그가 열려 있어요.",
        "talk_hint_saved": "#{n}의 다음 시도에 쓸 메시지를 저장했어요. 실시간 로그가 열려 있어요.",
        "talk_queue_failed": "#{n}에 메시지를 보내지 못했어요: {error}. 실시간 로그는 열린 채로 둬요.",
        "talk_no_handle": "Terminal은 핸들을 주지 않아서 정지 검사는 계속돼요.",
        "talk_manual": "터미널을 열 수 없어요. 직접 실행하세요: {command}",
        "talk_ended": "#{n} 대화 터미널이 닫혀 정지 시계를 다시 시작했어요",
        "talk_run_changed": "터미널을 여는 동안 #{n}이 새 실행을 시작했어요. fa lanes talk {n}을 다시 실행하세요.",
        "talk_session_starting": "#{n}이 새 작업자 세션을 시작하는 중이에요. 1분 뒤에 메시지를 다시 보내세요.",
        "talk_record_unreadable": "#{n}의 대화 기록을 읽을 수 없어요({path}). 그 터미널을 닫고 파일을 지운 뒤 다시 실행하세요.",
    },
}
