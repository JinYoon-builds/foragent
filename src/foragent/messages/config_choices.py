"""Invalid review/checks settings: where the value came from and how to fix it (#2734)."""

MESSAGES = {
    "en": {
        "config_choice_invalid": "{source}: {setting}: {value} is not a valid choice; use {choices}.",
        "config_choice_removed": (
            "{source}: {setting}: {value} was removed from Foragent and no longer works; use {choices}."
        ),
        "config_choice_recovery": "To fix it, run `fa init {flags}` or edit {source} to set {edits}.",
        "config_choice_or": " or ",
    },
    "ko": {
        "config_choice_invalid": "{source}: {setting}: {value}는 쓸 수 없는 값이에요. {choices} 중에서 골라요.",
        "config_choice_removed": (
            "{source}: {setting}: {value}는 Foragent에서 없어져서(removed) 더는 동작하지 않아요. {choices} 중에서 골라요."
        ),
        "config_choice_recovery": "고치려면 `fa init {flags}`를 실행하거나 {source}에서 {edits}로 바꿔요.",
        "config_choice_or": " 또는 ",
    },
}
