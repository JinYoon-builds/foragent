---
name: issue-record-cache
description: issues.json 해석 캐시(#588) 구조, 리뷰가 막은 두 결함 종류, 복사 비용 실측
metadata:
  type: project
---
`state_issues._issue_record()`는 (inode, size, mtime_ns) 버전별 해석 결과를 프로세스 전역 `_RECORDS`에 둔다(공유 객체, 변경 금지). 읽기 전용 복사본은 `_issue_view()`, 잠금 안 쓰기 기준은 `_issue_snapshot()`(항상 파일을 새로 해석, 캐시 미사용).
**Why:** 리뷰가 두 결함 종류로 막았다. ① 쓰기 기준을 캐시에서 가져오면 같은 크기·mtime 복원 제자리 편집을 덮어쓴다. ② stat→read_text 사이 교체·동시 미스로 같은 버전을 두 번 해석한다. 해결은 열린 파일의 fstat으로 버전과 내용을 묶고, 미스는 threading.Lock 안에서 다시 확인한다. 실측(2.5MB): json.loads 12ms, deepcopy 14ms, 재귀 dict/list 복사 3ms라 deepcopy는 쓰지 않는다.
**How to apply:** 새 조회는 필요한 값만 `_issue_record()`에서 뽑고, 잠금 안 읽기-고치기-쓰기는 `_issue_snapshot()`만 쓴다. 회귀 테스트는 `tests/test_state.py`의 `test_locked_write_reads_the_current_file_not_the_read_cache` 외 2개.
