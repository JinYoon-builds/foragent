---
name: broken-publish-preview-empty-files
description: fa order publish --dry-run prints an empty "파일:" line when the plan has no reference_files (after #2161 removed spec files)
metadata:
  type: project
---

Goal: preview a new order before approval.
Start: fa f2c89a48, foragent repo, plan with spec_markdown, one issue (lane "order"), no reference_files.
Repro: `fa order publish PLAN.json --dry-run`.
Observed: second line is "파일: " with nothing after it; the preview never says the spec goes to the parent issue body. With reference_files the line lists them.
Reported 2026-11-07; CONFIRMED by second session on 5ffe33ab same day (trailing space only after "파일:"). Remove once the line is omitted or explained.
