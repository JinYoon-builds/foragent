---
name: settings-runner-unit-tests
description: #308 runner_unit_tests team setting in fa settings and the settings window; locked tests load a Config dump with max_parallel null
metadata:
  type: project
---
- 새 팀 설정은 `settings.SETTINGS`에 한 줄 추가하면 `fa settings list/get/set`과 설정 창(`watch_sections/settings/__init__.py`)이 함께 따라온다. 창의 이름·값 문구는 `settings_window_label_<key>`·`settings_window_value_<key>_<value>` 키로 찾는다. 설정 수를 세는 일반 시험 3개(test_settings.py 2개, test_watch_settings.py 1개)를 함께 갱신한다.
- 설정 하나의 고유 화면 로직은 `watch_sections/settings/<key>.py`(KEY·`value_text(value, repo_root, language)`→문자열 또는 None)에 둔다. 창 `_rows()`가 패키지 안 모듈을 찾아 쓴다. `watch_sections/` 바로 아래 일반 모듈은 화면 구역으로 발견되어 ORDER가 필요하므로 거기 두지 않는다. 공유 `__init__.py`에 `if setting.key == ...` 분기를 넣거나 `src/foragent/`에 화면 문구 모듈을 두면 리뷰가 view-parallel 위반으로 FAIL을 낸다(#308 review-1).
- runner_unit_tests의 auto는 `run_checks.has_pull_request_ci`로 ci/all을 정한다(러너의 `trusts_ci`와 같은 판정).
- `test_settings_window.settings_case`는 `asdict(Config)`를 그대로 덤프해 `max_parallel: null`이 들어간다. 이 fixture로 `load_config`를 직접 부르는 고정 시험이 있으면 config가 null을 "생략"으로 읽어야 한다(#308에서 거부를 제거, test_config의 `[None-max_parallel]` ID는 유지하고 기대값만 바꿈).
