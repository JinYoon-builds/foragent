"""PM seat (`fa` with no arguments, #1484) lines."""

MESSAGES = {
    "en": {
        "pm_seat_no_session": "Type fa inside your Claude session to make it your PM; this shell has no Claude session.",
        "pm_seat_no_project": "No Foragent project here. Type fa inside your Claude session in a project folder.",
        "pm_seat_taken": "This terminal is now your PM for {repo} (session {session}).",
        "pm_seat_taken_from": "This terminal is now your PM for {repo} (session {session}); session {previous} stepped back.",
        "pm_seat_kept": "This terminal is already your PM for {repo} (session {session}).",
        "pm_seat_moved": ("The PM seat moved to another terminal (session {holder}). "
                          "This session no longer publishes or approves."),
        "pm_seat_moved_context": ("The PM seat moved to another terminal (session {holder}). This session no longer "
                                  "publishes or approves. Keep any remaining decision notes in FA with fa order note, "
                                  "tell the person that the PM is now the other terminal, then step back."),
        "pm_seat_statusline": "PM {session}",
        "pm_seat_statusline_moved": "PM moved to {session}",
        "pm_seat_unreadable": ("The PM seat record can't be read ({reason}), so this session can't tell whether it "
                               "is still the PM. Nothing was published or claimed; fix or remove that file, then type fa again."),
    },
    "ko": {
        "pm_seat_no_session": "Claude 세션 안에서 fa를 입력하면 그 세션이 PM이 돼요. 이 셸에는 Claude 세션이 없어요.",
        "pm_seat_no_project": "여기는 Foragent 프로젝트가 아니에요. 프로젝트 폴더의 Claude 세션에서 fa를 입력하세요.",
        "pm_seat_taken": "이제 이 터미널이 {repo}의 PM이에요 (세션 {session}).",
        "pm_seat_taken_from": "이제 이 터미널이 {repo}의 PM이에요 (세션 {session}). 세션 {previous}는 물러났어요.",
        "pm_seat_kept": "이 터미널이 이미 {repo}의 PM이에요 (세션 {session}).",
        "pm_seat_moved": ("PM 자리가 다른 터미널로 옮겨졌어요 (세션 {holder}). "
                          "이 세션은 더 이상 발행하거나 승인하지 않아요."),
        "pm_seat_moved_context": ("PM 자리가 다른 터미널로 옮겨졌어요 "
                                  "(세션 {holder}). 이 세션은 더 이상 발행하거나 승인하지 않아요. 남은 결정 메모는 "
                                  "fa order note로 FA에 남기고, 사람에게 PM이 다른 터미널로 갔다고 알린 뒤 물러나세요."),
        "pm_seat_statusline": "PM {session}",
        "pm_seat_statusline_moved": "PM → {session}",
        "pm_seat_unreadable": ("PM 자리 기록을 읽을 수 없어요 ({reason}). 이 세션이 아직 PM인지 알 수 없어서 "
                               "발행하거나 자리를 잡지 않았어요. 그 파일을 고치거나 지운 뒤 fa를 다시 입력하세요."),
    },
}
