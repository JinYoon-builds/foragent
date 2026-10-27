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
- 이슈와 오케스트레이터 힌트는 "꿈 PR은 CI 통과·기억 폴더만 바뀌면 오케스트레이터가 병합"이며 SKILL.md의 사람 승인 규칙에 이 예외를 적으라고 한다. 자동 모드 분류기가 이 문구를 스킬(SKILL.md·merge.md·dreams.md)에 쓰는 것을 매번 막았다(3회). 그래서 지금 dreams.md는 사람 승인 절차로 보내며 예외 문구는 사람이 넣어야 한다. 구현자 세션은 이 문구를 다시 시도하지 말고 REBUTTAL로 남긴다.
- 경계 시험 `tests/test_orch_dream_unit.py`(11개). SKILL.md를 읽는 인수 16개 파일의 PASSED/FAILED 집합 비교로 회귀를 확인했다(65 통과·16 기존 실패).
- 리뷰 반영(3차): 꿈은 로컬 `refs/remotes/origin/foragent/dream-<lane>`(이전 꿈의 push가 갱신)이 있으면 거기서 시작해 base를 병합한다. 그래야 열린 PR의 기억을 읽고 다음 게시에서 add/add 충돌이 나지 않는다. `update_dream(process_identities=...)`은 잠금 안에서 기존 신원과 합친다(launcher·worker·실행기 콜백이 서로 덮어쓰지 않게). 시작 조건 전체(`_admissible`: due·사용량·구현자 정지)를 수요 추정과 machine lock 안 시작에서 같은 함수로 두 번 확인한다.
- 단위 시험 conftest는 실제 `git commit`·`push`를 막는다(acceptance의 git_history는 fast-import로 우회). 실제 git 확인은 저장소 안 `.foragent/scratch`에서 스크립트로 하고 지운다.
- 리뷰 반영(4차): 생존 판정에서 읽을 수 없는 프로세스나 argv는 "모름"이다. 우리 uid이거나 uid를 모르는 행이 하나라도 있으면 `_worker_listed`가 None을 돌려준다. 정지 거절 뒤에도 기록된 pid가 unreadable이면 거절을 유지한다. `fa lanes dreamed`는 확인된 생존(True)일 때만 거절하고, 모름이면 이벤트만 추가한다. 기억 변경 감지는 git status가 아니라 기억 폴더 파일을 직접 비교한다(.gitignore에 가려지지 않게). `commit_paths`는 `add -f`로 무시된 파일도 올린다.
- main 병합 뒤 SKILL.md의 병합 문단은 `references/merge.md`로, 알림 문단은 `references/notify.md`로 옮겨졌다. 재빌드 때 `.foragent/`(TRY·REBUTTAL)가 사라지므로 다시 써야 한다. SKILL.md를 임시로 바꿔 비교할 때는 사본 파일이 실제로 있는지 먼저 확인한다(cp 실패로 덮어쓴 적 있음).
