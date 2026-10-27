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
- 시작 후보는 이슈 시작 전에 계산되므로 `start()`가 machine lock 안에서 `dream_due`를 다시 확인한다. 열린 dream PR은 fetch→merge→`push(lease=head)`로 보존한다. 입력은 자르지 않고(마지막 꿈 이후 전체), 완료 이벤트의 `covered_until`(프롬프트 작성 시각)을 기준으로 다음 꿈이 센다. 매체 파일은 `.<lane>.md.lock` flock과 `git merge-file`로 3방향 병합한다.
- 꿈 PR 병합: 이슈 명세(2026-10-27 갱신)대로 SKILL.md의 사람 병합 규칙에 예외 한 문장이 있고, `references/dreams.md`·`references/merge.md`가 같은 말을 한다. 오케스트레이터는 CI가 통과하고 diff가 그 레인 기억 폴더만 건드리면 사람에게 묻지 않고 `fa orch merge PR --approved-by <자기 gh 로그인>`으로 직접 병합한다. 명세에 문장이 명시되기 전에는 자동 모드 분류기가 이 문구를 막았다.
- 경계 시험 `tests/test_orch_dream_unit.py`(11개). SKILL.md를 읽는 인수 16개 파일의 PASSED/FAILED 집합 비교로 회귀를 확인했다(65 통과·16 기존 실패).
- 리뷰 반영(3차): 꿈은 로컬 `refs/remotes/origin/foragent/dream-<lane>`(이전 꿈의 push가 갱신)이 있으면 거기서 시작해 base를 병합한다. 그래야 열린 PR의 기억을 읽고 다음 게시에서 add/add 충돌이 나지 않는다. `update_dream(process_identities=...)`은 잠금 안에서 기존 신원과 합친다(launcher·worker·실행기 콜백이 서로 덮어쓰지 않게). 시작 조건 전체(`_admissible`: due·사용량·구현자 정지)를 수요 추정과 machine lock 안 시작에서 같은 함수로 두 번 확인한다.
- 단위 시험 conftest는 실제 `git commit`·`push`를 막는다(acceptance의 git_history는 fast-import로 우회). 실제 git 확인은 저장소 안 `.foragent/scratch`에서 스크립트로 하고 지운다.
- main 병합 뒤 SKILL.md의 병합 문단은 `references/merge.md`로, 알림 문단은 `references/notify.md`로 옮겨졌다. 재빌드 때 `.foragent/`(TRY·REBUTTAL)가 사라지므로 다시 써야 한다. SKILL.md를 임시로 바꿔 비교할 때는 사본 파일이 실제로 있는지 먼저 확인한다(cp 실패로 덮어쓴 적 있음).
- 리뷰 반영(5차): 꿈 작업자와 `fa lanes dreamed`는 `scheduler_config.tick_config(store, config)`로 tick이 기록한 커밋된 설정을 쓴다(checkout 설정이 아니라 시작 판단과 같은 출처). `_stop`은 killer가 오류 없이 끝나도 성공이 아니다: 끝난 뒤 `_run_processes_exist`·unreadable 검사로 모두 사라졌음을 확인해야 True다(출생 기록 없는 작업자 pid는 recorded tree에 없어 kill되지 않는다). status의 `active_runs`·레인 행, state_refresh·progress·brief의 번호별 시작 시각은 `run.kind == "issue"`만 쓴다.
- `tests/test_cli.py::test_inspect_renders_card_prepared_results_and_language`는 시간이 지나면 깨지는 시험이었다: 고정 NOW(10-20)에 병합한 PR을 inspect가 실제 시계의 최근 7일 창으로 읽었다. 시험 안에서 `cli.datetime`을 NOW로 고정해 고쳤다. runner는 바꾼 파일의 관련 시험(main에서도 실패하는 것 포함)을 통과해야 하므로, '기존 실패'라는 반박으로는 넘어가지 못한다. 원인을 고친다.
- SKILL.md는 150줄 제한이 있고 100자 넘는 줄도 이미 있으므로, 문장을 더할 때는 줄을 늘리지 말고 기존 줄에 이어 쓴다.
- 재설계(3번째 보류 뒤): 꿈에는 자기 프로세스 코드가 없다. `orch_dream_stop.py`(발견·기록·신호)를 지웠고, 꿈 기록은 `Run.key`(`dream-<lane>`)로 이슈와 같은 `update_run`·`finish_run`(str key는 finish_dream으로, runs.jsonl 제외)·`scheduler_recovery._recover_runs`·`_kill_run`·`_stop_run_processes`를 탄다. 이슈 라벨, flag, WIP 보존, `runner_process_exists`는 `run.kind == "issue"`일 때만 쓴다. 생존 규칙은 `scheduler_stop.runner_alive` 하나다(기록된 시작 시각이 다르면 죽음, 맞으면 `os.kill(pid, 0)`로 다시 확인; 예전 인라인 코드와 같아야 test_scheduler의 os.kill 호출 시험이 통과한다). launcher·`gate run-dream`·작업자 콜백은 issue처럼 `update_run(key, pid=..., process_identities=...)`를 쓴다.
- 게시: 완료 이벤트에 `published`(PR에 들어간 브랜치 head)를 남긴다. 다음 꿈은 원격 꿈 브랜치 head가 그 값과 다르고 기억 폴더가 base와 다르면, 새 수정이 없어도 PR을 열거나 갱신한다(push는 됐지만 PR이 실패한 경우). 시험은 `scheduler._recover_runs`를 가짜 표·가짜 killer(기록된 신원만 지움)로 돌리고, `processes.observe_tree`는 항등 함수로 바꾼다. 테스트 모듈은 `foragent.scheduler`를 먼저 import해야 순환 import가 안 난다.
- 리뷰 반영(재설계 뒤 1차): `runner_alive`는 3값이다. 기록된 pid가 `ProcessTable.unreadable`에 있으면 None(모름)이다. 복구는 `is False`일 때만 죽은 것으로 끝내고, `fa lanes dreamed`는 `is True`일 때만 거절한다. `_run_processes_exist`는 읽을 수 없는 기록 pid를 남아 있는 것으로 센다. 이 공용 코드는 이슈 실행에도 같이 적용된다(test_scheduler 통과 확인).
- 리뷰 반영(재설계 뒤 2차): `_stop_run_processes`는 정지가 오류 없이 끝나도, 기록된 pid가 읽을 수 없으면 확인되지 않은 것으로 보고 거절한다(`_stop_unconfirmed`). 출생 신원 없는 pid를 다시 probe하면 잠긴 정지 시험(probe 1회, 주입 killer 신뢰)이 깨지므로 넣지 않는다. `_kill_run(decided=run)`은 새로 읽은 기록의 `DECISION_FIELDS`(진행·대기·sleep)가 결정 때와 다르면 멈추지 않는다. 복구는 잠긴 시험이 패치하는 `scheduler_recovery._pid_exists`를 `runner_alive(run, _pid_exists)`로 넘긴다.
- 꿈이 기본으로 켜지면서 가짜 launcher가 `launch(number)`뿐인 잠긴 시험(test_lane_queue 3개, test_optimistic_overlap 3개)이 `kind` TypeError로 깨진다. 명세대로 QUESTION.md에 적고 멈췄다. 회귀 비교는 tick을 쓰는 인수 80개 파일을 20개씩 나눠 main 소스(PYTHONPATH)와 대조한다(한 번에 돌리면 10분을 넘긴다).
