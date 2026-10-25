---
name: project-450-pr-title
description: #450 PR title/squash subject from issue title; why merge_pr subject is optional and GhTracker reads live PR title
metadata:
  type: project
---
#450: PR 제목은 항상 `<이슈 제목> (#N)`. 병합 직전 `run_publish.retitle_for_merge()`가 현재 이슈 제목과 PR 제목을 비교해 다르면 `update_pr`로 바꾼다(gate·review-only·orch merge 공유). squash 제목은 `GhTracker.merge_pr()`가 `subject`가 없으면 `gh pr view --json title`로 실제 PR 제목을 읽어 `--subject`로 넘긴다.

**Why:** 잠긴 인수 시험(`test_gate_merge_reinstall.py` 등)이 `merge_pr(number, *, expected_head_sha, method)`로 교체해 `subject` 인자를 받지 않는다. 그래서 호출자는 retitle이 rate-limit 아닌 오류로 실패했을 때만 `subject=`를 넘긴다(helper가 `{}` 또는 `{"subject": title}` 반환).

**How to apply:** `merge_pr` 호출에 새 키워드를 늘 넘기면 잠긴 fake가 깨진다. 새 merge 인자는 선택적으로만 넘기거나 tracker 안에서 구한다. orch merge의 `_merge_title()`: 연결 이슈 없으면 PR 자체 제목, batch head는 `Batch: <parent> (#N)`. `_batch_pr`도 `(#N)`을 붙인다.

리뷰 지적(재시도 3): gate의 `_open_or_update_pr()`·`publish_refresh`도 기존 PR에 제목을 쓴다. 여기서 제목 실패가 나면 병합까지 못 간다. `_update_open_pr()`가 non-rate-limit 실패 시 현재 PR 제목으로 base·본문만 다시 갱신하고 `attempt.pr_title`에 실제 제목을 남긴다. 병합 때 `retitle_for_merge(current_title=attempt.pr_title)`가 다시 시도한다. 교훈: "실패해도 계속"은 같은 쓰기를 하는 모든 이전 단계에도 적용된다.

리뷰 지적(재시도 4): 이전 제목으로 다시 갱신하는 시도도 같은 오류(HTTP 500 지속)로 실패할 수 있다. 이제 non-rate-limit 실패는 모두 로그만 남기고 `attempt.pr_title=None`(모름)으로 계속한다. 병합 때 `retitle_for_merge(current_title=None)`가 다시 제목을 바꾸고, 실패하면 subject를 명시한다. base 재지정 실패는 `_final_merge_inputs()`가 실제 PR base를 비교해 `base-changed`로 막으므로 잘못된 base 병합은 없다. 관련 묶음을 한 번에 돌리면 30분 백그라운드 한도에 걸릴 수 있으니 묶음을 나눠 foreground로 돌린다.
