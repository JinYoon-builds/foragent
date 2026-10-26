---
name: project-108-say
description: #108 orch --say person line — person line by #N: summary format, helper in activity section, section-package constraint
metadata:
  type: project
---

- `append_event` writes `say` only when non-blank. The view (`watch_sections/activity.person_line`) uses say → localized `orch_say_<kind>` line when kind is in `SAY_KINDS` and summary starts with `#{issue}: ` (the verbs' format since #30, so pre-`--say` journal lines are localized too) → raw summary.
- **Why:** reviewer rejected a "say key present" rule (old verb events stayed raw); `tests/acceptance/test_view_watch.py` writes `retry` events with free-form summaries (`handled-event-N`) and expects them shown. The `#N: ` summary format satisfies both.
- Every module in `src/foragent/watch_sections/` must have ORDER/render (discovery imports all). Reviewer rejected a helper outside the package (`orch_say.py`), so the line helper lives in the section that draws it (`activity.py`), not in a new pseudo-section.
- Korean lines after `#{issue}` avoid particles (을/를 depends on the number).
- zsh does not word-split `$files`; wrap multi-file `fa test` runs in `bash -c`.
