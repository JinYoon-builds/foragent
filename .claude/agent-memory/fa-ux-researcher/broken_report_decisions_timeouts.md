---
name: broken-report-decisions-timeouts
description: fa brief --report counts orchestrator-timeout needs-human issues as person decisions; --full lists them with no question or options
metadata:
  type: project
---

Goal: read the /fa three-line report and know what is mine to decide.
Start: foragent checkout, fa ae155622, many issues stuck on CI timeouts.
Repro: `fa brief --report` then `fa brief --report --full` (both advance the shared report time).
Observed 2026-11-06: short says "결정 기다리는 일 11개" and "결정할 일을 하나씩 여쭤볼게요"; --full lists "#2321 ... 결정이 필요해요" lines with no question/options. Every listed issue had needs-human reason=orchestrator-timeout; only PM-forwarded items (#2332, #2161) had real options. Conflicts with Jin's 2026-11-04 rule that timeouts never count as person questions.
Status: CONFIRMED by second session 2026-11-06 12:51Z on be706efa (14 listed: 2 PM questions + 12 timeouts); re-sent via fa orch request. Remove once the count shows only real questions.
