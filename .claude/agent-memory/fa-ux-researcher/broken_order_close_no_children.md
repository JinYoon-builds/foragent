---
name: broken-order-close-no-children
description: An order with no sub-issues (re-approve order) closes within a minute with "이 주문은 적용됐고 (끝남 0개)" while the issues it acts on stay open
metadata:
  type: project
---
Goal: the owner reads the order issue to learn whether the ordered work shipped.
Start: a re-approve order with plan map={} (example #1846, re-approving #1475 #1491 #1791 #1793).
Repro (also #2177, closed in 16 s, #1475 shipped 2 h later; #1990 closed in 16 s, #1952 shipped 34 h later via PR #2185): `gh issue view 1846 --json createdAt,closedAt,subIssues` → closed 47 s after creation, 0 sub-issues; last comment '이 주문은 적용됐고 열린 하위 이슈가 없어서(끝남 0개) 주문 기록을 닫았어요.' The four issues closed 11–43 h later.
Failure: the report says "applied" before anything shipped, and the order-ended UX check fires early. Confirmed by a second session 2026-11-06; the visible order body also never names the four issues. Different cause from [[broken-order-close-replaced-child]].
**How to apply:** recheck on the next re-approve or close-only order; remove once such an order stays open or names its still-open issues.
