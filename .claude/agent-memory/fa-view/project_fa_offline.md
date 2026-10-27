---
name: project-fa-offline
description: /fa startup must make no GitHub call (#756); how the update offer still reaches /fa
metadata:
  type: project
---

`fa brief --report` and every /fa startup command must not call GitHub (#756). `update_check.check` can look up a release and `star.offer` runs `gh auth status`; on `--report` (report on or off) use `update_check.cached` and skip the star request. The /fa skill runs its second command as `FORAGENT_UPDATE_CHECK=off fa brief --json`; the report carries the cached offer as its last line and JSON `update`. Plain `fa brief --json` keeps its daily check (locked `test_update_check` contract).

**Why:** A reviewer rejected the rebuttal "the contract needs fa brief's check" twice. A redesign that keeps both rules (offline /fa, contract untouched) passed.
**How to apply:** When the same finding comes back after a rebuttal, redesign so that both constraints hold. Do not rebut again. Audit every command a skill runs, not only the one the review names.
