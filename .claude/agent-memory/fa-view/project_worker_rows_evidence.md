---
name: worker-rows-and-evidence
description: #477 레인 화면의 작업자별 번호 줄과 D67 증거 나이(심장 박동·로그·PID) 판정 위치와 함정
metadata:
  type: project
---
#477에서 `×N` 배수를 없애고 작업자마다 도로 한 줄(`<레인> 1`, `<레인> 2`)을 그린다. 순서는 `watch_layout._clones`가 `(started_at, number)`로 정하고 status가 running인 실행만 센다. 대기열·끝낸 일 줄은 1번 줄에만 붙는다. 활동 표는 레인 이름을 그대로 쓰고 작업자가 둘 이상이면 같은 시작 순서를 따른다.

같은 이슈의 Done-when에 `test_view_evidence`(D67 화면 쪽)가 함께 있었다. 구현: `watch_data.evidence`가 `<state>/orch-heartbeat.json`·`gate-heartbeat.json`의 `at`과 `logs/issue-N.log` mtime 나이를 Snapshot `heartbeats`·`log_ages`에 담는다. 오케스트레이터 30초(lanes `_overview`), gate 15분(summary 첫 줄), 로그 `min(10, stall_minutes)`분 이상이면 `watch_quiet`(`_activity`의 `log_seconds`). 로그가 없으면 예전 `last_activity_seconds`의 "멈춘 듯"을 쓴다.

**Why:** 고정 시험끼리 문구가 갈린다. `test_view_watch`는 로그 없이 "멈춘 듯"을, `test_stall_visibility`는 죽은 PID 줄에 "○"를, `test_view_evidence`는 "작업자가 없어요"와 "⚠"를 요구한다. 그래서 `watch_runner_gone`은 "○ ⚠ 작업자가 없어요"다.
**How to apply:** 화면 문구를 바꿀 때 세 시험을 함께 돌린다. heartbeat 쓰기(test_heartbeats, `fa orch watch`·gate)는 #477 범위 밖이라 구현하지 않았다. 기준본 비교는 `.foragent/base`에 `git archive HEAD src`를 풀고 `_build.py`를 복사한 뒤 `PYTHONPATH=.../src fa test ...`로 한다(저장소 안, gitignore 대상).

리뷰 보완: 도로 이름 칸은 `min(19, road_width // 3)`칸으로 자른다. 번호 줄은 `_road(..., worker=N)`이 ` N ▸ `를 먼저 남기고 레인 이름만 `…`로 줄인다. 통째로 자르면 `orchestration` 같은 긴 이름에서 번호가 사라진다. 회귀 검증: `tests/test_watch_workers.py -k long_lane_name`.

리뷰 보완 2: status JSON은 레인 대표 상태 하나뿐이다(status.py 변경 금지). 그래서 작업자별 상태는 `watch_data.collect`가 로컬 이슈 라벨로 `Snapshot.worker_states`를 만든다(needs-human → blocked → working, collect_lanes와 같은 순서). 도로 번호 줄과 활동 표의 다른 작업자 줄이 이를 쓴다. 회귀 검증: `tests/test_watch_workers.py -k own_state`. `test_view_done_state` 5개는 기준본에서도 실패한다.
