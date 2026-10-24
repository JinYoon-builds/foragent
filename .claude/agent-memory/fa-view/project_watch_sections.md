---
name: watch-sections-compositor
description: #353 lane view is composed from foragent/watch_sections/<name>.py modules; rules for adding sections and verifying no screen change
metadata:
  type: project
---

#353 `watch_layout._frame`은 `foragent.watch_sections.__path__`를 매 프레임 `pkgutil.iter_modules`로 찾아 ORDER 순으로 쌓는다. 구역: summary 10, stage 20, lanes 30, activity 40, detail 50, help 60. 비지 않은 구역 사이에 빈 줄 하나를 넣고, `GAP = False`(help)는 빈 줄 없이 붙인다. 문맥에는 tick·width·selected·now·layout 외에 `top`(구역 첫 줄 y)과 `hits`(클릭 영역 목록)가 있다.

**Why:** 고정 `test_view_sections`는 watch_layout.py 소스에 화면 i18n 키 문자열(SECTION_KEYS)이 하나도 없어야 하고, 패키지 안 모든 비패키지 모듈이 int ORDER와 render를 가져야 한다. 그래서 공용 도우미(`_widths`·`_split`·`_clones`·`_lane_name`·`_lane_issues`, `_has_work`·`_activity`·PICK)는 watch_layout에 두고, 패키지 안에 도우미 모듈을 두지 않는다. `render_detail`·`_stage`는 각각 detail·stage 구역으로 옮겼고 watch.py가 다시 내보낸다.

**How to apply:** 새 화면 구역은 `watch_sections/<name>.py`에 ORDER·render만 추가한다. 화면 불변 검증은 `git archive HEAD src`를 저장소 안 `.foragent/base`에 풀고(`_build.py` 복사) `.venv/bin/python`으로 PYTHONPATH만 바꿔 여러 너비·tick·언어의 render_frame과 layout hits를 출력해 `cmp`한다(이번 336개 동일). 편집 가능 설치가 있어도 PYTHONPATH가 우선함을 `__file__`로 확인했다. Done when 묶음 + stall_visibility·pixel_art는 169 passed.
