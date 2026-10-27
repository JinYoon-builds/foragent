# Order card

Do not turn every sentence into a new issue. Keep one durable order draft in `docs/specs/` and use
the project's decision log for settled context. Use the repository's `fa-order` skill to clarify
only unresolved product choices, split work into independently verifiable issues and prepare a plan
JSON. When clear, propose one card with new issues grouped by lane, dependencies, Done when checks,
and any new lanes (human name, paths, skills).

[edit] revises the draft; [later] keeps it for later without publication. Validate with
`fa order publish PLAN.json --dry-run` before publishing (use selective approval flags if
requested). Existing issue changes go in the plan's `edits` and need the user's re-approval.
Summarize what was recorded or published and resume watching without asking the user to prompt you
again. Retain compact pending decisions, attempts and draft references across turns; re-read
current facts when handling the next event.
