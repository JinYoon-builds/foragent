---
name: closed-pr-watermark
description: 닫힌 PR 증분 읽기(#589) 구조 — merged_prs.json 목록+watermark, getattr로 GhTracker만 사용
metadata:
  type: project
---
`RecordingTracker._merged_prs`는 추적기에 `merged_prs_updated_since`가 있을 때만(GhTracker) 증분 읽기를 한다. FakeTracker 등은 예전 `list_prs("merged")` 경로를 그대로 쓴다. 병합 PR 레코드(number/head/base/merged_at/url/updated_at)와 watermark(새로 고침 시작 시각)는 상태 디렉터리의 `merged_prs.json`에 둔다(issues.json을 키우지 않으려고). `refresh_local_issues` 성공 뒤에만 기록한다.
**Why:** 인수 테스트가 "전체 목록과 같은 결과"를 요구한다. 보관 목록 ∪ 새 페이지를 updated_at 내림차순으로 정렬하면 전체 목록과 같은 PR 목록이 되어 `complete_prs=True` 경로를 그대로 쓸 수 있다.
**How to apply:** 중단 판정은 필터 전 원본 페이지의 updated_at으로 한다. 시각을 알 수 없는 항목은 오래됐다는 근거가 아니다. 여유는 `_WATERMARK_MARGIN`(10분).

**함정(재시도로 확인):** 인수 테스트의 gh 흉내 응답(`test_local_view.py` 등)에는 `updated_at`이 없는 PR이 있다. 새 응답 필드는 `.get`으로 읽고, 예전 `list_prs`가 요구하던 필드(number·head.ref·base.ref·html_url)만 필수로 둔다. gh를 흉내 내는 인수 테스트 파일은 `grep -rlE "_run_gh|GhTracker\(|pulls" tests/acceptance`로 모아 파일별로 돌린다.
