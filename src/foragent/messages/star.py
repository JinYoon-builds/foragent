"""The one-time star request in `fa brief`."""

MESSAGES = {
    "en": {
        "star_question": "Is Foragent useful? Star {repo} on GitHub? Reply with fa brief --star yes or no",
        "star_thanks": "Thanks! You starred {repo}.",
        "star_declined": "Got it. Foragent won't bring this up again.",
        "star_failed": "Couldn't add the GitHub star. Nothing was recorded.",
        "star_not_asked": "No open request to answer, so --star was ignored.",
        "star_json": "--star can't be used with --json.",
    },
    "ko": {
        "star_question": "Foragent가 쓸 만한가요? GitHub에서 {repo}에 별(star)을 줄까요? fa brief --star yes 또는 no로 답해 주세요",
        "star_thanks": "고마워요! {repo}에 star를 줬어요.",
        "star_declined": "알겠어요. 다시 묻지 않을게요.",
        "star_failed": "GitHub star를 주지 못했어요. 답은 기록하지 않았어요.",
        "star_not_asked": "답할 요청이 없어서 --star를 무시했어요.",
        "star_json": "--star는 --json과 함께 쓸 수 없어요.",
    },
}
