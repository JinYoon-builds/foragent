---
name: project-hold-sources
description: Where blocked/needs-human holds and returns to ready are recorded with a time (#756 fa brief --report)
metadata:
  type: project
---

#756 `brief_report._resolved_holds` pairs every timed hold record with every timed return to ready.
- 보류 시작: 사건 `escalate`·`retries-exceeded`·`orchestrator-timeout`·`stalled`(두 경로 모두 blocked) + `flags/<n>.json`의 `flagged_at`(`actions.flag`가 blocked 붙일 때마다 씀).
- ready 복귀: 사건 `retry`·`crash-restart`·`stall-restart`, `resolve`의 `requeued: true`(orch.resolve가 `_retry`를 부를 때 기록), `approvals/<n>.json`의 `approved_at`, gate 머지.
- 기록 없는 경로(runner SystemExit blocked, GitHub에서 직접 라벨 변경)는 REBUTTAL로 남김.

**Why:** 리뷰가 종류 목록 누락(stalled, resolve)을 "결함 종류"로 지적함. 사건 종류만 보지 말고 라벨을 붙이는 모든 producer(`grep add_labels(... BLOCKED|NEEDS_HUMAN|READY`)를 대조해야 함.
**How to apply:** 사건 기록에서 상태 변화를 추론할 때는 그 상태를 쓰는 모든 코드 경로를 먼저 나열하고, 시각 있는 기록이 있는지 확인한다.
