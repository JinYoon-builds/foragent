"""Per-issue skill catalog: implementer prompt section and the `Skills used:` report.

The prompt and the PR body are English, so only English templates exist here.
"""

MESSAGES = {
    "en": {
        "skills_catalog_heading": "## Available skills",
        "skills_catalog_intro": ("These approved skills may help with this issue. Choose the ones that fit; "
                                 "read a skill's SKILL.md before you use it."),
        "skills_catalog_empty": "No other approved skills are available for this issue.",
        "skills_catalog_item": "- {name}: {description} ({path})",
        "skills_catalog_report": ("End your final message with one line naming every skill you used, "
                                  "including lane skills: `Skills used: <names>` (comma-separated), "
                                  "or `Skills used: none`."),
        "skills_used_line": "Skills used: {names}",
        "skills_used_none": "none",
        "skills_used_not_reported": "not reported",
    },
}
