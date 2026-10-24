# View work in parallel: per-feature message files and screen sections

Evidence (2026-10-24): every queued view issue declares src/foragent/watch.py and src/foragent/i18n.py, so the gate's file-overlap rule runs them one at a time, and all add keys to one MESSAGES dict. watch.py (86 lines) and i18n.py (82 lines) are already small; the shared declared files are the problem. Same lesson as #338.

- messages-registry: strings live in foragent/messages/<feature>.py (MESSAGES with en and ko), found by discovery over the package path; i18n.text() and i18n.MESSAGES keep working; a key in two modules is an error naming both.
- view-sections: the running lane view is composed from foragent/watch_sections/<name>.py modules (ORDER and render), found by discovery; existing sections move unchanged; an empty section adds no blank line.
- Existing screens and wording stay as they are: the existing view tests that pass today are part of Done when. No golden frames are locked, because later view issues change screens on purpose.
- The 14 queued view issues now declare their own section, message and test files and wait for both issues (real: those files exist only after them).

Jin approved 2026-10-24 ~16:35 KST.
