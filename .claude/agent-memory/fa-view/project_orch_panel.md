---
name: orch-panel-note-lines
description: #172 orchestrator box note lines (now/why/next/where, 사람이 볼 것), wide-box width, inspect count file rules
metadata:
  type: project
---

#172 메모 줄은 `watch_sections/orch_panel.py`(ORDER 35, render는 `[]`)의 `lines()`가 만들고 `lanes._overview`가 상태 줄 바로 뒤에 넣는다. 데이터는 `Snapshot.panel`(watch_data.collect → `orch_panel.collect`)이다.

- 고정 시험은 160칸에서 "라벨+값"이 한 화면 줄에 있어야 해서 28칸 상자로는 불가능했다. `_widths`에서 140칸 이상만 상자를 36칸으로 키웠다(좁은 화면은 그대로).
- 고정 시험은 막힌 일이 있어도 하트비트가 살아 있으면 "지켜보는 중"을 요구한다. 기존 `문제 푸는 중` 줄은 그대로 두고 그 앞에 `지켜보는 중` 줄만 더한다(합치면 리뷰가 기존 표시 변경으로 지적함). 폭 확대는 셀 수 측정(26칸 안쪽 vs 29~31칸 요구)으로 REBUTTAL에 반박했다.
- `inspect-count.json`은 `inspect_data.collect`가 쓰되 상태 폴더가 있을 때만 쓴다. `tests/test_inspect.py`·`test_cli.py`가 inspect가 상태 폴더를 만들지 않음을 검사한다.
- 명시한 `--orca-title`은 ORCA_TERMINAL_HANDLE 없이도 orca(handle None)로 기록한다. 명시한 `--tmux-pane`이 같이 오면 tmux가 이긴다.
- 기준본 비교로 새 실패 0개, 기존 실패 4개 해결(관련 묶음 448 passed·26 failed, 기준 437·30).
- 리뷰가 같은 "기존 표시 변경" 지적을 반박 뒤에도 반복하면, 고정 시험을 통과하는 대안이 없다는 것을 셀 수로 확인한 뒤 QUESTION.md(질문별 A/B/C, Evidence에 이슈 문장·시험 행·기준본 행)로 결정을 올린다. 코드는 시험을 통과하는 쪽으로 둔다.
- 2026-10-27 오케스트레이터 결정: Q1·Q2 모두 A(140칸↑ 36칸 고정, `지켜보는 중` 줄 추가). 고정 시험이 이슈 문장과 충돌하는 곳의 계약이다. 리뷰가 다시 지적하면 이 결정을 REBUTTAL에 인용한다. 재시도 때 작업 트리가 다시 만들어지면 코드는 WIP 커밋에 남고 `.foragent/` 파일만 사라진다.
- 결정이 이슈 본문에 반영되지 않으면 리뷰는 "결정이 Goal과 모순"이라며 계속 지적한다. REBUTTAL에 힌트 원문 인용, 셀 수 근거, 본문이 결정보다 오래됐다는 사실(updatedAt)을 함께 적는다. 이슈 본문은 직접 고치지 않는다(실행 중 해시 불일치).
- 결정을 인용한 REBUTTAL도 리뷰가 "Goal과 모순"으로 세 번째 거절했다. 결정이 이슈 본문에 없으면 반박으로는 끝나지 않으므로, QUESTION.md로 본문 갱신(A)을 요청한다.
- main 병합(2026-10-27): main이 Snapshot에 `merged_at`을 더해 watch_data.py가 충돌했다. `merged_at` 뒤에 `panel`을 두고 생성자 끝에 `merged_at, orch_panel.collect(store, now)` 순으로 넘긴다. 결정이 Goal에 반영된 뒤에는 남은 QUESTION.md를 지워야 spec-question으로 다시 멈추지 않는다. `test_view_usage_line` 5개 실패는 미구현 #115 범위다.
