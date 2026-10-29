---
name: usage-line-footer
description: #115 usage line section placement under 24-row budget; SHORT/APART layout attributes; locked tests that pin the last frame row
metadata:
  type: project
---

#115 사용량 줄은 `watch_sections/usage.py`(ORDER 65, GAP False, GAP_AFTER False, APART ("gates",), KEEP, SHORT)다. 사실은 `watch_data.meter()`가 새로고침마다 한 번 읽어 `Snapshot.meter`에 담는다(machine_runs의 OSError는 running을 빼서 화면이 active_runs 수로 대신함). `m`은 `watch_mouse.handle_key`가 상자가 닫혀 있을 때 `quota_mode`에서 뒤집고, `state["quota_mode"]=(모드, 누른 시각)`으로 다음 새로고침 전에도 보이게 한다.

**Why:** 고정 시험끼리 바닥 줄을 서로 다르게 고정한다. `test_view_usage_line`은 마지막 줄에 사용량, `test_squirrel_running` 20명은 줄인 화면 마지막 줄에 `q`, `test_view_sections`는 기준 20줄+4줄=정확히 24줄(덧붙인 마지막 구역이 살아야 함), `test_installed_gates_visible`은 게이트 줄 앞뒤 빈 줄. 이슈 글("도움말 바로 앞", "짧은 화면엔 초대 줄만 빠짐")은 시험과 충돌해 REBUTTAL로 남겼다. `render_frame`은 height 없이도 24줄(F)로 줄이므로 KEEP 아닌 줄은 쉽게 빠진다.

**How to apply:** 바닥에 줄을 더하는 화면 작업은 먼저 위 네 시험과 `tests/test_watch_acorns.py`·`test_watch_squirrel.py`의 바닥 줄 기대값을 확인한다. 줄인 화면에서는 도움말(short)이 열쇠 글자 뒤에 `usage.lines()`를 이어 붙이고 usage는 []를 반환한다. 레이아웃의 짧은 화면 분기는 이름이 아니라 `SHORT` 속성이다. 기준본 비교는 `.foragent/base`에 `git archive HEAD src`를 풀고 `PYTHONPATH`로 같은 파일 목록을 돌려 실패 ID를 `comm`으로 대조했다(`test_lane_dream` capacity 2개는 간헐 실패). 백업은 존재하는 폴더에 두고 `cp` 성공을 확인한 뒤 변형한다(없는 `.foragent/`에 백업해 변형이 쌓인 적 있음).
