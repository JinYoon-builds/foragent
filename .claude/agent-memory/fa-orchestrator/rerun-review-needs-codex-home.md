---
name: rerun-review-needs-codex-home
description: fa orch rerun-review runs the reviewer in this session's environment, so export the live CODEX_HOME first or the review comes back UNUSABLE
metadata:
  type: project
---

`fa orch rerun-review` starts the reviewer from this session, with this session's `CODEX_HOME`. That value is stale here (the 626b3cfd account path no longer exists), so without an export the review ends UNUSABLE in seconds with empty output.

**Why:** on 2026-11-02 my reruns of #1597 and #1105 failed this way after a real network drop, and the issue records then showed two causes mixed under one verdict.

**How to apply:** export the c7c83cab account path in the same command before any rerun-review. An empty review file plus "Error finding codex home" in the issue log means this, not a code defect.
