---
name: prepare-kept-lock-mismatch
description: Statement-route edits (goal-only or lock-only) stop in fa order prepare when the writer lists an unchanged existing lock; #2558 is the fix
metadata:
  type: project
---
On 11-07, a goal-only narrowing of a statement-route issue (#2558, #2560) stopped in `fa order prepare` with "작성자의 답과 작업 사본이 쓴 파일 목록에서 달라요: <existing locked file>". A lock-only correction of a statement issue (#2546) is refused at publish with "no reviewed tests", even though `--dry-run` passes. Legacy-route issues (no `statements` in `fa order edit N`) publish after a preread, with no prepare.

**Why:** #2558 (prepare counts existing locks) is the fix, so #2558 cannot re-approve itself. Splitting a prepared plan also invalidates the review: the review is bound to the whole plan.

**How to apply:** Check `fa order edit N` for `statements` before you choose a route. Until #2558 merges, send statement-route edits through the PM self-bottleneck shortcut, or wait. Prepare the plan exactly in the form you will publish. Use `fa order prepare --json`: text mode crashed on 11-07 (prepare_row TypeError). Related: [[prove-locks-can-pass]].
