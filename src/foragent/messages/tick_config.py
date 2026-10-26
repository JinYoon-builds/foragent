"""Gate tick notices about the committed .foragent.yml on the base branch."""

MESSAGES = {
    "en": {
        "tick_config_fallback": "{ref} .foragent.yml is missing or invalid; this tick uses the local .foragent.yml.",
        "tick_config_key_ignored": "{ref} .foragent.yml sets {key} differently; this tick keeps the local {key}.",
    },
    "ko": {
        "tick_config_fallback": "{ref}의 .foragent.yml이 없거나 잘못됐어요. 이번 tick은 local .foragent.yml을 써요",
        "tick_config_key_ignored": "{ref}의 .foragent.yml이 {key}를 다르게 정했어요. 이번 tick은 local {key}를 그대로 써요",
    },
}
