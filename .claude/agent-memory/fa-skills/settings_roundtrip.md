---
name: settings-roundtrip
description: How lane commands edit .foragent.yml by lines (config.LaneSettings) and why ruamel round-trip was rejected
metadata:
  type: project
---

`fa lanes add/skills`는 `config.LaneSettings`로 `.foragent.yml`을 줄 단위로 고친다 (#233). 위치는 `yaml.compose()` 노드의 `start_mark`/`end_mark`로 찾고, 바꿀 줄만 고친 뒤 `check()`가 `safe_load(결과) == 기대값`을 확인한다. 다르면 쓰지 않고 `ConfigError`를 낸다. `check()`는 스킬 설치 전에 부른다.

**Why:** ruamel.yaml 왕복은 리뷰에서 거절됐다. 파일 전체를 다시 출력해서 `repo:    o/r`의 공백이 바뀌고, 블록 목록 항목 주석이 사라지고, 끝 주석 뒤에 `[]`를 써서 잘못된 YAML이 나왔다.

**How to apply:**
- 바이트 보존 요구에는 재직렬화 라이브러리 대신 줄 편집 + 다시 읽기 확인을 쓴다.
- PyYAML 함정: 빈 null 값(`skills:`)은 start==end이고 위치가 믿을 수 없다. 키 줄의 `:` 뒤에 쓴다. 블록 컬렉션의 end_mark는 뒤 주석을 지나 다음 토큰에 있으니 마지막 자식으로 내려가 끝 줄을 찾는다.
- 두 번째 리뷰 지적: CRLF는 `read_bytes`/`write_bytes`로 지킨다(text 모드가 LF로 바꾼다). `{...}` 형식 레인·lanes에는 마지막 값 뒤에 `, key: value`만 끼운다(전체 재작성 금지). 지운 목록 항목은 `- item`만 같은 폭 공백으로 바꿔 주석의 열·바이트를 지킨다.
- 회귀: `tests/test_skills_lanes.py -k settings_document`.
