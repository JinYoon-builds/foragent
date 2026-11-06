---
name: broken-order-close-replaced-child
description: Order-close report says "적용됐고 … 완료" when the child was closed as replaced by another PR
metadata:
  type: project
---
Goal: the owner reads the order issue to learn whether the order's change shipped.
Start: an order whose only child was closed by `fa orch close` as replaced (e.g. #1759 → child #1760, replaced by PR #1970; #1760's own PRs #1772 and #1780 closed and not merged).
Repro: read the last comment on the order → "이 주문은 적용됐고 … 완료: #1760". The child has stateReason COMPLETED. The order issue does not mention the replacement.
Failure: the report says "replaced" nowhere, so the owner thinks this order's own change merged. Reported 2026-11-06 (confirmed). Recheck on the next order whose child is closed as replaced.
