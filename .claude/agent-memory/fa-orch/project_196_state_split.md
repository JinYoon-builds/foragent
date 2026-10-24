---
name: project-196-state-split
description: state.py facade split (#196) — module layout, _state call-time lookup rule, and how to merge later main changes into it
metadata:
  type: project
---

state.py는 facade이고 구현은 `state_base`(쓰기 원시 함수·`_write_mapping`/`_write_run`)·`state_locks`(state/merge/notify/tick 잠금, 재진입 `hold_tick_lock`, 기준선 캐시 load/lock)·`state_local`(order progress·승인·flag·inspect·usage·tidy)·`state_runs`(run 기록·run-start 저널·lane/executor/github 일시정지·history)·`state_issues`·`state_refresh`·`state_records` 믹스인에 있다.

**규칙:** "facade에서 patch 가능"은 메서드뿐 아니라 모듈 전역(REFRESH_INTERVAL·Run·json·load_record 등)도 뜻한다. 각 하위 모듈은 맨 끝에서 `import foragent.state as _state`를 하고 본문은 `_state.X`로 호출 시점에 찾는다. 회귀 시험 `tests/test_state.py::test_facade_globals_patched_on_foragent_state_reach_the_split_implementation`.

**병합 방법(2026-10-25):** main이 옛 단일 state.py를 고치면 충돌 블록에서 facade를 유지하고, `git diff $(git merge-base HEAD origin/main) origin/main -- src/foragent/state.py`의 각 hunk를 해당 믹스인으로 옮긴다. 옮긴 뒤 main의 StateStore 메서드 소스와 분할본 소스를 `_state.` 제거 후 텍스트 비교하고, main의 모듈 수준 이름이 모두 `hasattr(foragent.state, …)`인지 확인한다. main이 `Run(**x)`를 `load_record(Run, x)`로 바꾸어 Run 대역은 함수가 아니라 `@dataclass(frozen=True)` 하위 클래스여야 한다.

**재시도(2026-10-25):** runner가 "충돌 표식 해결"을 요구해도 HEAD가 이미 병합 커밋일 수 있다. `grep '^<<<<<<<'`과 `git merge-base --is-ancestor origin/main HEAD`로 먼저 확인한다. "Check timed out"은 기준선 인수 시험 묶음의 시간 제한이며 시험 실패가 아니다.
