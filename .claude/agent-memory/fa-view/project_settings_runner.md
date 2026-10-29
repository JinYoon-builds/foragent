---
name: settings-runner-unit-tests
description: #308 runner_unit_tests team setting in fa settings and the settings window; locked tests load a Config dump with max_parallel null
metadata:
  type: project
---
- 새 팀 설정은 `settings.SETTINGS`에 한 줄 추가하면 `fa settings list/get/set`과 설정 창(`watch_sections/settings/__init__.py`)이 함께 따라온다. 창의 이름·값 문구는 `settings_window_label_<key>`·`settings_window_value_<key>_<value>` 키로 찾는다. 설정 수를 세는 일반 시험 3개(test_settings.py 2개, test_watch_settings.py 1개)를 함께 갱신한다.
- `auto` 값의 실제 적용(ci/all)은 `settings_runner.auto_text`가 `run_checks.has_pull_request_ci`로 계산한다(러너의 `trusts_ci`와 같은 판정).
- `test_settings_window.settings_case`는 `asdict(Config)`를 그대로 덤프해 `max_parallel: null`이 들어간다. 이 fixture로 `load_config`를 직접 부르는 고정 시험이 있으면 config가 null을 "생략"으로 읽어야 한다(#308에서 거부를 제거, test_config의 `[None-max_parallel]` ID는 유지하고 기대값만 바꿈).
