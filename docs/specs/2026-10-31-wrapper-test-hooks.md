# #1026 wrapper test (2026-10-31)

Baseline regression: the locked statusline wrapper test stripped only the SessionStart hook; #1026 adds the UserPromptSubmit hook its Goal requires. The helper now strips every Foragent hook.
