---
name: usage-line-footer
description: #115 usage line section placement under 24-row budget; SHORT/APART layout attributes; locked tests that pin the last frame row
metadata:
  type: project
---

#115 사용량 줄은 `watch_sections/usage.py`(ORDER 65, GAP False, GAP_AFTER False, APART ("gates",), KEEP)이고 ` · q 끝내기`로 끝난다(도움말 문구에서는 q를 뺐다). 폭을 넘으면 항목 사이에서 줄바꿈한다(이어 붙이거나 잘리게 두면 리뷰가 KEEP 위반으로 반려). 스펙 갱신(2026-10-29): 사용량이 마지막 줄, 도움말은 바로 위, 초대 줄도 짧은 화면에서 유지. 사실은 `watch_data.meter()`가 새로고침마다 한 번 읽어 `Snapshot.meter`에 담는다(machine_runs의 OSError는 running을 빼서 화면이 active_runs 수로 대신함). `m`은 `watch_mouse.handle_key`가 상자가 닫혀 있을 때 `quota_mode`에서 뒤집고, `state["quota_mode"]=(모드, 누른 시각)`으로 다음 새로고침 전에도 보이게 한다.

**Why:** 고정 시험끼리 바닥 줄을 서로 다르게 고정한다. `test_view_usage_line`은 마지막 줄에 사용량, `test_squirrel_running` 20명은 줄인 화면 마지막 줄에 `q`, `test_view_sections`는 기준 20줄+4줄=정확히 24줄(덧붙인 마지막 구역이 살아야 함), `test_installed_gates_visible`은 게이트 줄 앞뒤 빈 줄. 이슈 글("도움말 바로 앞", "짧은 화면엔 초대 줄만 빠짐")은 시험과 충돌해 REBUTTAL로 남겼다. `render_frame`은 height 없이도 24줄(F)로 줄이므로 KEEP 아닌 줄은 쉽게 빠진다.

**How to apply:** 바닥에 줄을 더하는 화면 작업은 먼저 위 네 시험과 `tests/test_watch_acorns.py`·`test_watch_squirrel.py`의 바닥 줄 기대값을 확인한다. 도움말·사용량·게이트 사이 빈 줄은 하나만 가능해서(컴포지터 24줄 + 게이트 앞뒤 빈 줄) 다른 게이트 목록만 사용량 아래에 온다. 레이아웃의 짧은 화면 분기는 이름이 아니라 `SHORT` 속성이다. 기준본 비교는 `.foragent/base`에 `git archive HEAD src`를 풀고 `PYTHONPATH`로 같은 파일 목록을 돌려 실패 ID를 `comm`으로 대조했다(`test_lane_dream` capacity 2개는 간헐 실패). 백업은 존재하는 폴더에 두고 `cp` 성공을 확인한 뒤 변형한다(없는 `.foragent/`에 백업해 변형이 쌓인 적 있음).

#115 3차: 리뷰가 "다른 게이트가 있어도 사용량이 마지막"을 요구했다. 사용량 ORDER 90을 GAP False로 두면 `test_installed_gates_visible:198`이 실패하고, GAP True로 두면 `test_view_sections` 3개가 실패한다. 이를 직접 돌려 증명한 뒤 QUESTION.md로 올렸다(추천: 198줄을 고쳐 게이트 → 도움말 → 사용량 순서). 답이 오면 그 순서로 바꾸고 `tests/test_watch_usage.py`의 게이트 예외 시험을 뒤집는다.
