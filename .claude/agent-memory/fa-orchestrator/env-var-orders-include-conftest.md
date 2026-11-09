---
name: env-var-orders-include-conftest
description: when an order makes the product read a new environment variable, put tests/conftest.py in Files; otherwise the harness guard holds the PR (11-09 #2861, cost a Files edit + retry)
metadata:
  type: feedback
---

#2861 made the product read CLAUDE_CODE_SESSION_ID. The implementer correctly added it to the env vars tests/conftest.py
clears, but conftest was outside Files, so the gate held the PR as test-harness-changed; a resolve verdict does not clear
that guard, only an order edit adding the path (order #2871) and a retry.

**Why:** the harness guard blocks any Files-outside change to test configuration until Files names it (precedent #2697, PM q61).
**How to apply:** when an order's behaviour depends on a new env var, flag, or global the test suite must isolate, add
`tests/conftest.py` to that issue's Files at order time.
