---
name: broken-order-close-split-cancelled
description: Order close report says "취소(계획 안 함)" for a child split into several replacements; no pointer to the new issues/order (confirmed 2026-11-07)
metadata:
  type: project
---
Goal: the owner reads the order issue to learn whether the ordered work shipped, was dropped or moved.
Start: an order whose only child was split into several issues under a new order (#2157→#2158→#2481–#2483 under #2480; #2311→#2312→#2484–#2487).
Repro: `gh issue view 2157 --comments` → last comment "… 취소(계획 안 함): #2158." It never names #2480 or #2481–#2483. Those issues are approved and running. `fa orch close --help`: --superseded-by takes one value.
Failure: the owner sees dropped work. A single replacement works ("대체됨: #2343 -> #2383", see [[broken-order-close-replaced-child]]).
Reported 2026-11-07 via fa orch request (saved as reported; id not yet in inbox, find by #2158) after second-session confirmation; reproduced again same day (owner: orchestrator).
Variant (order #2293, child #2158 split into #2481–#2483, all three OPEN): the close note says "이 주문은 적용됐고 열린 하위 이슈가 없어서(끝남 0개) 주문 기록을 닫았어요" and names no replacement. `fa dashboard 2293 --once` shows only "마지막 리뷰: 아직 없어요". `fa dashboard 2158 --once` still says "Too big for one session; split it in order". Confirmed in a second session and reported 2026-11-07 (find it by #2293). Reproduced again and re-sent 2026-11-07; the earlier send did not appear in `fa orch inbox` (grep #2293 found nothing).
Rechecked 2026-11-07 at #2311's scheduled end check (3b60a244): the close note on #2311 still says "취소(계획 안 함): #2312" and does not name #2484–#2487, which are OPEN. `fa dashboard 2312 --once` shows the closed issue as "사람 확인을 기다리며 멈췄어요 / awaiting PM decision". This is the same variant as #2158, so I did not send it again.
Recheck: at the next split order's end. Remove this entry once the close report names the replacements.
After #2567 merged (fa 6f9952f6, 2026-11-07): `fa orch close --superseded-by` now takes several numbers. But `fa dashboard 2158 --once` / `2312 --once` still show "split it in order" / "awaiting PM decision" and no replacements. Those children were closed by the order-split path (comment marker `foragent:replaced plan=`). Confirmed in a second session and sent via fa orch request 2026-11-07 (owner: orchestrator). Recheck: after the follow-up fix merges.
Reproduced again 2026-11-07 (6f9952f6) with the same output. The earlier post-#2567 send was not in `fa orch inbox` (grep 2567/2481/2484 found nothing), so I re-sent it via fa orch request (saved as reported; find it by "#2566 / issue #2567 end check"). Owner: orchestrator. Recheck: after the follow-up fix merges.
