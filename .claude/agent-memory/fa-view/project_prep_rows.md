---
name: prep-rows
description: #171 order preparation rows in the lane view — where facts come from, section order, test pitfalls
metadata:
  type: project
---
- 준비 사실의 원본은 `fa order progress`(order.progress → `<state>/order-progress.json`, `store.order_progress()`)다. 화면은 읽기만 한다.
- `watch_data.collect`가 `Snapshot.prep`에 넣고, 발행 번호를 `titles`의 shown에 더해 제목을 얻는다. 구역은 `watch_sections/prep.py`(ORDER 4: 머리줄 3과 레인 줄 5 사이), 문구는 `messages/prep.py`.
- `watch._clean`이 공백을 접으므로 줄 앞 들여쓰기는 효과가 없다. 표 정렬은 `watch._columns` 너비로 한다.
- 고정 시험은 제목 줄에 `?`가 없어야 하고(물은 칸 탐색이 "Login"+"?" 첫 줄), `#1` 작업 줄이 한 번만 있어야 한다.
- 기준본에서도 실패: test_squirrel_done 2개, test_view_ux_copy 2개(범위 밖).
- 검증: `fa test tests/acceptance/test_prep_scenes.py tests/test_watch_prep.py`.
- 리뷰 보완(결함 종류): ① 높이 부족 시 구역 통째 제거 → `KEEP = True`. ② 뒤쪽 사실이 앞 글에 밀려 잘림 → 제목 줄은 단계·시험 수·발행 번호를 남기고 발행 제목, 주문 제목 순으로 줄인다. 표는 묶음 칸이 먼저 줄어 다섯 칸을 지킨다. ③ 0을 없음으로 취급 → 시험 수는 tests 단계 이후면 0/0도 보인다.
