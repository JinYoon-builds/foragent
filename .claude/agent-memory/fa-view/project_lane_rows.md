---
name: lane-rows-167
description: #167 lane rows replaced roads/work table; header/rows/height-budget design, locked-test conflicts and pitfalls
metadata:
  type: project
---

#167 레인 화면: `watch_sections/squirrels.py`(ORDER 3, 머리줄: 첫 실행·모두 쉼이면 큰 다람쥐, 아니면 say/오늘 집계 한 줄)와 `lanes.py`(ORDER 5, GAP False, 작업자별 줄·대기 ⏳·쉬는 💤). 아래 구역 순서: stage 20, progress 21, done 22, summary 23, orch_panel 35(오케 상자, 140칸↑ SIDE로 옆에), activity 40(say 없는 사건만), detail 50, help 60, gates 70. 문구는 `messages/squirrel.py`, 설정 `view_glyphs`(emoji|ascii|auto).

`_frame`은 기본 `height=24`(SCREEN_ROWS). `KEEP = True` 구역(머리줄·레인 줄·detail·help)만 남기고 나머지를 아래부터 뺀다. 빠지면 help에 `short=True`. 각 구역은 한 번만 그리고(top=0) 히트는 조립 때 옮긴다 — 고정 test_view_sections가 render 호출 횟수와 width==120을 검사하기 때문. `render_frame`은 `_frame`에 tick/width/selected/now(+layout)만 넘겨야 한다(test_watch_split). 문맥 `rows=True`면 progress가 "진행 중 #N" 줄을 뺀다(고정 ETA 시험은 progress.render를 단독 호출해 그 줄을 요구).

**Why:** 옛 도로·작업 표를 요구하는 고정 시험 14개(view_evidence 2·stall_visibility 2·view_waiting_phases 6·view_compact 2·view_watch 2)는 새 시험의 "번호는 한 줄에만"과 정면 충돌해 `.foragent/QUESTION.md`로 올렸다(추천: 오케스트레이터가 옛 시험을 레인 줄 기준으로 갱신).

**How to apply:** 화면 줄 수를 늘리는 변경은 24줄 예산(5레인+FIRST/LAST 추가 구역, 20작업자)에 걸린다. `○`는 이제 도토리 기호라 "○ 없음"으로 멈춤을 검사하는 일반 시험은 경고 문구로 검사한다. 🐿은 `watch_cells.WIDE_EMOJI`로 2칸. 검증: `fa test tests/acceptance/test_squirrel_running.py tests/test_watch*.py tests/test_config.py`.

결말(2026-10-27): 오케스트레이터가 QUESTION에 선택지 1로 답하고, 옛 시험 14개를 레인 줄에서 증거를 찾도록 고쳐 Done when에 넣었다. 구현은 그대로 97개 통과. 재시도 때 작업 트리가 다시 만들어져도 작업은 브랜치(WIP 커밋+병합)에 남고 `.foragent/TRY.md`·try 스크립트만 다시 쓰면 된다.
