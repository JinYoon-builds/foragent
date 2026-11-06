---
name: broken-order-close-replaced-child
description: Order-close report says "적용됐고 … 완료" for a child closed as replaced; code fixed by PR #2250, old records still wrong
metadata:
  type: project
---
Goal: the owner reads the order issue to learn whether the order's change shipped.
Start: an order whose child was closed as replaced (e.g. #1759 → child #1760, replaced by PR #1970).
Repro: read the last comment on the order → "이 주문은 적용됐고 … 완료: #N". The child has stateReason COMPLETED and no replacement note.
Status 2026-11-06: r840 was accepted. PR #2250 (merged 2026-11-06T05:19Z) makes `fa orch close N --superseded-by M` close as not planned and say "Replaced". The fix covers only new closes made with --superseded-by.
Still broken: pre-fix records. Order #1033 says 완료 for #1043, which was split into #1262–#1264/#1312 and closed COMPLETED with no pointer to them. An addendum proposing a one-time record fix was sent 2026-11-06 through fa orch request ("reported"; it may not show in the inbox until a second session confirms it).
Recheck: on the next order whose child is closed with --superseded-by, check that the roll-up says Replaced. If it does and the #1033/#1043 records are fixed or left as history, remove this entry.
