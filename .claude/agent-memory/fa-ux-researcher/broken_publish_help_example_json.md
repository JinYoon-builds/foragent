---
name: broken-publish-help-example-json
description: fa order publish --help PLAN.json example is not valid JSON (raw newline in reference_files value)
metadata:
  type: project
---

Goal: hand-write PLAN.json by copying the `fa order publish --help` schema example.
Start: foragent 0.1.0 (ed15ca1a), 2026-11-06.
Repro: copy the block from `{` after "PLAN.json schema" to the first `}` line; `python3 -I -c 'import json,sys;json.load(open(sys.argv[1]))' FILE`.
Failure: JSONDecodeError "Invalid control character" line 13 col 79 — reference_files "# Approved login screen" then a real newline then `..."`. With that one newline written as \n, the whole example parses (closes entry from #2169 is fine).
Status: reproduced again and reported via fa orch request 2026-11-06 (request report "reported"; an earlier note said reported but the inbox had no entry). Next: orchestrator decides; order lane owns the help text.
