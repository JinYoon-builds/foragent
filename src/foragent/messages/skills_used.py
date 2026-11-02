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
        "skills_catalog_file": ("{count} approved skills may help with this issue. The list, with each skill's "
                                "description and SKILL.md path, is in {path}."),
        "skills_catalog_report": ("End your final message with one line naming every skill you used, "
                                  "including lane skills: `Skills used: <names>` (comma-separated), "
                                  "or `Skills used: none`."),
        "default_skills_heading": "## Default skills for this role",
        "default_skills_intro": "These skills are configured for this role. Read a skill's SKILL.md when it fits the work.",
        "default_skills_item": "- {name}: {description} ({path})",
        "default_skills_bare_item": "- {name} ({path})",
        "default_skills_missing": "- {name}: not available here",
        "skills_used_line": "Skills used: {names}",
        "skills_used_none": "none",
        "skills_used_not_reported": "not reported",
    },
}
