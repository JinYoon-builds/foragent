---
name: broken-orch-role-help-pointer
description: fa orch role --help says role names are "as listed by fa role", but fa role cannot list (only add/set/remove)
metadata:
  type: project
---

`fa orch role --help` positional `name` says "as listed by fa role"; `fa role` has only add/set/remove and errors without an action. The real list is `fa orch roles` (crashed on 2026-11-06 with Lock order inversion, #2283/PR #2289).
Confirmed 2026-11-06 on fa 0.1.0 (4be849b8); reported via fa orch request.

**Why:** a person who calls a role by hand cannot find valid role names from help.
**How to apply:** retest `fa orch role --help`; workaround is `fa orch role nosuchrole --why x`, which prints the role names.
