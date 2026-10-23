---
name: settings-roundtrip
description: How lane commands edit .foragent.yml without losing comments (ruamel SettingsDocument pitfalls)
metadata:
  type: project
---

`fa lanes add/skills`는 `config.SettingsDocument`(ruamel.yaml 왕복 편집)로 `.foragent.yml`을 고친다 (#233). 다른 writer(`fa init`, `set_profile`)는 아직 `yaml.safe_dump`다.

함정:
- `load_yaml_guess_indent`는 자체 로더를 써서 `preserve_quotes`가 빠진다. 들여쓰기 값만 얻고 로드는 따로 한다. 반환 indent는 블록 목록 들여쓰기이지 매핑 들여쓰기가 아니다.
- ruamel은 끝 주석(빈 줄+`# ...`)을 가장 깊은 마지막 항목에 붙인다. 새 키를 붙이면 주석이 새 키 앞에 남으므로 `_take_tail`/`_put_tail`로 옮긴다. 줄 끝 주석 토큰은 `#`로, 뒤따르는 줄 토큰은 `\n`으로 시작한다. 줄 끝 부분은 제자리에 둔다.
- 회귀: `tests/test_skills_lanes.py -k settings_document`. 검증: `fa test tests/acceptance/test_lanes_skills_keeps_comments.py`.
