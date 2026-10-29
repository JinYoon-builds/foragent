---
name: usage-line-footer
description: #115 usage line is always the last frame row (gates → help → usage); 24-row compositor budget pitfalls; Snapshot.meter and the m key
metadata:
  type: project
---

#115 최종 배치(2026-10-29 선택지 B): 다른 게이트(70, 앞에 빈 줄) → 도움말(80, GAP False) → 사용량(90, GAP False, GAP_AFTER False, KEEP). 사용량 줄은 ` · q 끝내기`로 끝나고 도움말 문구에서는 q를 뺐다. 폭을 넘으면 항목 사이에서 줄을 바꾼다(이어 붙이거나 잘리게 두면 리뷰가 KEEP 위반으로 반려). 초대 줄도 KEEP이다. 사실은 `watch_data.meter()`가 새로고침마다 한 번 읽어 `Snapshot.meter`에 담는다. `m`은 `watch_mouse.handle_key`가 상자가 닫혀 있을 때만 `quota_mode`에서 뒤집고, 화면은 `state["quota_mode"]=(모드, 누른 시각)`으로 바로 보여 준다.

**Why:** 고정 `test_view_sections` 컴포지터 시험은 모든 구역을 한 줄씩 그려 24줄 안에 들어야 한다. 구역 패키지의 모든 모듈이 한 줄을 차지하므로 아무것도 그리지 않는 도우미 모듈(#190의 dream)도 예산을 먹는다. 그래서 그 도우미는 `src/foragent/watch_dream.py`로 옮겼다. `test_installed_gates_visible`은 게이트 줄 앞 빈 줄과, 뒤에 빈 줄·화면 끝·바닥 두 줄 중 하나를 요구한다.

**How to apply:** 바닥에 줄을 더하거나 구역 모듈을 추가하기 전에, 기준본에서 컴포지터 시나리오의 줄 수를 센다(모든 구역 render를 한 줄로 바꾸고 `_frame(height=None)`). 24면 여유가 없다. 도우미는 패키지 밖 `watch_*.py`에 둔다. 기준본 비교는 `.foragent/base`에 `git archive <main> src`를 풀고 `PYTHONPATH`로 같은 파일 목록을 돌려 실패 ID를 `comm`으로 대조한다. 백업은 존재하는 폴더에 두고 `test -s`로 확인한 뒤 변형한다.
