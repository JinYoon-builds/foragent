---
name: project-450-pr-title
description: #450 PR title/squash subject from issue title; why merge_pr subject is optional and GhTracker reads live PR title
metadata:
  type: project
---
#450: PR 제목은 항상 `<이슈 제목> (#N)`. 병합 직전 `run_publish.retitle_for_merge()`가 현재 이슈 제목과 PR 제목을 비교해 다르면 `update_pr`로 바꾼다(gate·review-only·orch merge 공유). squash 제목은 `GhTracker.merge_pr()`가 `subject`가 없으면 `gh pr view --json title`로 실제 PR 제목을 읽어 `--subject`로 넘긴다.

**Why:** 잠긴 인수 시험(`test_gate_merge_reinstall.py` 등)이 `merge_pr(number, *, expected_head_sha, method)`로 교체해 `subject` 인자를 받지 않는다. 그래서 호출자는 retitle이 rate-limit 아닌 오류로 실패했을 때만 `subject=`를 넘긴다(helper가 `{}` 또는 `{"subject": title}` 반환).

**How to apply:** `merge_pr` 호출에 새 키워드를 늘 넘기면 잠긴 fake가 깨진다. 새 merge 인자는 선택적으로만 넘기거나 tracker 안에서 구한다. orch merge의 `_merge_title()`: 연결 이슈 없으면 PR 자체 제목, batch head는 `Batch: <parent> (#N)`. `_batch_pr`도 `(#N)`을 붙인다.
