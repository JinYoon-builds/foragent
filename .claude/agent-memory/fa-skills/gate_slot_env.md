---
name: gate-slot-env
description: Gate/fa test pass FORAGENT_TEST_SLOT=held to children; capacity slot tests fail if conftest does not clear it
metadata:
  type: project
---

게이트와 `fa test`는 시험 명령에 `FORAGENT_TEST_SLOT=held`를 넘긴다. 이 값이 있으면 `capacity.hold_test_slot()`은 잠금을 건너뛴다. 그래서 `tests/conftest.py`의 `isolated_test_slots`가 이 변수를 지운다(#233에서 추가). 지우지 않으면 `test_capacity.py`의 슬롯 시험 3개가 게이트에서만 실패한다.

**How to apply:** 게이트에서만 실패하는 시험은 `FORAGENT_TEST_SLOT=held uv run pytest ...`로 재현해 본다.
