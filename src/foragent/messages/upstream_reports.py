"""Upstream error reports: the `fa init` question and `fa report` lines (#419)."""

MESSAGES = {
    "en": {
        "upstream_reports_question": (
            "May Foragent propose upstream reports about its own errors to {upstream}? "
            "You see each report in full and nothing is sent without your yes. [y/N] "),
        "upstream_reports_on": "Upstream reports: on. Foragent may propose a report; you approve each one.",
        "upstream_reports_off": "Upstream reports: off. Foragent won't ask again.",
        "upstream_reports_unanswered": "Upstream reports: not asked (no terminal); fa init will ask next time.",
        "report_not_allowed": (
            "Upstream reports are off. To allow them, set upstream_reports: true in {path}."),
        "report_bad_source": "Use --from flag:N or event:N (N is an issue number or a journal line).",
        "report_source_missing": "Nothing found for {source}.",
        "report_no_error": "{source} has no Python error to report.",
        "report_unknown": "No prepared report {report_id}. Run fa report propose first.",
        "report_already_sent": "Report {report_id} was already sent: {url}",
        "report_send_hint": "Nothing was sent. To send this report: fa report send {report_id} --yes",
        "report_send_question": "Send this report to {upstream}? [y/N] ",
        "report_declined": "Not sent.",
        "report_commented": "Added the report to the open upstream issue: {url}",
        "report_created": "Opened an upstream issue: {url}",
    },
    "ko": {
        "upstream_reports_question": (
            "Foragent 자체 오류를 {upstream}에 보내는 upstream report를 제안해도 될까요? "
            "보고서는 매번 전체를 보여 주고, 승인해야만 보내요. [y/N] "),
        "upstream_reports_on": "Upstream report: 켬. Foragent가 보고서를 제안하고, 보낼 때마다 승인을 받아요.",
        "upstream_reports_off": "Upstream report: 끔. 다시 묻지 않을게요.",
        "upstream_reports_unanswered": "Upstream report: 묻지 못했어요(터미널 없음). 다음 fa init 때 다시 물어요.",
        "report_not_allowed": (
            "Upstream report가 꺼져 있어요. 켜려면 {path}에 upstream_reports: true를 넣어 주세요."),
        "report_bad_source": "--from flag:N 또는 event:N을 쓰세요(N은 이슈 번호 또는 기록 줄 번호).",
        "report_source_missing": "{source}에서 찾은 기록이 없어요.",
        "report_no_error": "{source}에는 보고할 Python 오류가 없어요.",
        "report_unknown": "준비된 보고서 {report_id}가 없어요. 먼저 fa report propose를 실행하세요.",
        "report_already_sent": "보고서 {report_id}는 이미 보냈어요: {url}",
        "report_send_hint": "아무것도 보내지 않았어요. 보내려면: fa report send {report_id} --yes",
        "report_send_question": "이 보고서를 {upstream}에 보낼까요? [y/N] ",
        "report_declined": "보내지 않았어요.",
        "report_commented": "열린 upstream 이슈에 보고서를 덧붙였어요: {url}",
        "report_created": "upstream 이슈를 열었어요: {url}",
    },
}
