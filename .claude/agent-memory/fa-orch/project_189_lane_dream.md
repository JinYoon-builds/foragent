---
name: project-189-lane-dream
description: #189 lane dream design — dream Run records, due rule, worker, line-limit splits, test traps
metadata:
  type: project
---

- 꿈 기록은 `Run(kind="dream", number=0, lane)`이고 `runs/dream-<lane>.json`에 저장한다(`StateBase._run_path`). 기록 메서드(get/start/update/finish_dream)는 `state_base.py`에 있다: `state_runs.py`는 300줄 제한(test_state_split·test_cli_split)에 걸려 있다. runs.jsonl에는 넣지 않는다.
- 적격성 초기화의 유일한 근거는 orch-events의 `kind: dream-done`(+`lane`, `memories`) 이벤트다(`orch_dream.last_dream_at`). 머지 수는 `read_history()`+`finish_times()`에서 그 시각 이후 lane의 merged만 센다.
- tick: `orch_dream.startable(ctx, sample)`가 `ctx._dream_demand`를 정해 `_machine_budget`의 waiting에 1을 더한다(안 그러면 budget=demand라 두 번째 꿈이 슬롯을 못 얻음). `_machine_budget`은 `ctx._tick_budget`을 남기고, 꿈은 `_start_ready` 뒤에 그 값으로 시작한다. 레인 꿈 중엔 `_lane_held`가 그 레인 이슈 시작을 막는다.
- `processes.list_processes`·`observe_tree`는 psutil이라 `view_offline`(Popen을 git/sh/bash로 제한)에서도 쓸 수 있다. 시험 conftest가 `capacity.machine_root()`를 바꾸므로 media는 `Path.home()/.local/share/foragent/media`를 직접 쓴다.
- scheduler.py 250줄, 새 모듈 300줄 제한(test_scheduler_split). 워커는 `orch_dream_worker.py`로 분리했다. `capacity.py`·`run_publish.py`의 test_runner_split 초과는 base부터 있던 실패다.
- 리뷰 반영(2차): 꿈 정지는 `processes.recorded_tree(run.process_identities)` 안에서만 `kill_runner`를 부른다(없으면 기본 killer가 ValueError). 생존 판정은 3값(`orch_dream_stop.dream_liveness`): `ProcessTable.unreadable`·목록 실패는 None이고 tick은 그 경우 기록을 건드리지 않는다. 죽은 워커도 남은 실행기 트리를 먼저 멈춘 뒤 끝낸다. 워커는 `track_process_group(tree_callback=...)`로 실행기 신원을 기록한다.
- 시작 후보는 이슈 시작 전에 계산되므로 `start()`가 machine lock 안에서 `dream_due`를 다시 확인한다. 열린 dream PR은 fetch→merge→`push(lease=head)`로 보존한다. 입력은 자르지 않고(마지막 꿈 이후 전체), 완료 이벤트의 `covered_until`(프롬프트 작성 시각)을 기준으로 다음 꿈이 센다. 매체 파일은 `.<lane>.md.lock` flock과 `git merge-file`로 3방향 병합한다.
- 자동 모드 분류기는 스킬에 "사람 확인(AskUserQuestion) 없이 병합" 문구를 쓰는 것을 막는다. 승인 절차를 바꾸는 문구는 쓰지 말고 기존 SKILL.md 병합 절차로 보낸다.
- 경계 시험 `tests/test_orch_dream_unit.py`(11개). SKILL.md를 읽는 인수 16개 파일의 PASSED/FAILED 집합 비교로 회귀를 확인했다(65 통과·16 기존 실패).
- 리뷰 반영(3차): 꿈은 로컬 `refs/remotes/origin/foragent/dream-<lane>`(이전 꿈의 push가 갱신)이 있으면 거기서 시작해 base를 병합한다. 그래야 열린 PR의 기억을 읽고 다음 게시에서 add/add 충돌이 나지 않는다. `update_dream(process_identities=...)`은 잠금 안에서 기존 신원과 합친다(launcher·worker·실행기 콜백이 서로 덮어쓰지 않게). 시작 조건 전체(`_admissible`: due·사용량·구현자 정지)를 수요 추정과 machine lock 안 시작에서 같은 함수로 두 번 확인한다.
- 단위 시험 conftest는 실제 `git commit`·`push`를 막는다(acceptance의 git_history는 fast-import로 우회). 실제 git 확인은 저장소 안 `.foragent/scratch`에서 스크립트로 하고 지운다.
