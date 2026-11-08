---
name: broken-orch-close-help
description: fa orch close --help has no description/option help; missing issue gives raw gh 404 without "nothing changed" (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: maintainer reads help before closing a superseded issue with `fa orch close` (#1914, order #1912).
Start: /Users/apple/Dev/foragent, fa 0.1.0 (52e5b3fa).
Repro: `fa orch close --help` → no description; number/--superseded-by/--why bare (park/stop document theirs).
`fa orch close 999999 --superseded-by 1914 --why check` → only `foragent: gh: Not Found (HTTP 404)`, no number, no "아무것도 바꾸지 않았어요".
Reported via fa orch request 2026-11-07 (second-session confirm; no request id printed). Gate-busy first try is a correct refusal, not part of the bug. Recheck after the fix merges; remove when it works.
Merged/running refusal untested: auto mode blocks the GitHub write.
