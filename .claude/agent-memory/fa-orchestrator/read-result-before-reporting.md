---
name: read-result-before-reporting
description: Send the PM a status message only after reading the command's output, not in the same step as the command
metadata:
  type: feedback
---

Do not send a message about an action's outcome in the same step as the action. Run it, read the output, then report.

**Why:** on 2026-11-02 messages sent alongside a command were wrong three times: "load went down" (it was 16.56), "#1526 edit published" (it was refused), "retried #1511" (the command was refused). Each cost a correction to the PM.

**How to apply:** when a tool batch both acts and reports, split it. Say "doing X" if a message must go first.

**11-09:** I reported order 2026-11-09-blocked-by-existing as published because its sequencer line printed, but the line
after it was a refusal ("tests not reviewed together"); found 12 h later. After every sequencer run, grep its output for
"할 수 없어요|cannot|거절" and check `order_publications.requests` status, not just the "==" header.
