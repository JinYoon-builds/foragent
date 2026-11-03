---
name: read-result-before-reporting
description: Send the PM a status message only after reading the command's output, not in the same step as the command
metadata:
  type: feedback
---

Do not send a message about an action's outcome in the same step as the action. Run it, read the output, then report.

**Why:** on 2026-11-02 messages sent alongside a command were wrong three times: "load went down" (it was 16.56), "#1526 edit published" (it was refused), "retried #1511" (the command was refused). Each cost a correction to the PM.

**How to apply:** when a tool batch both acts and reports, split it. Say "doing X" if a message must go first.
