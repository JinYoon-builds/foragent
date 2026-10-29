---
name: project-435-silence-watchdog
description: #435 shared silence watchdog (foragent/watchdog.py) — where it is wired, conflicts with #431 tests, older tests left in QUESTION
metadata:
  type: project
---
`SilenceWatchdog.observe()` is the one decision. Commands: `run_logged(watchdog=)` polls every 5s and sends stdout to a private file, so harness log writes are not counted as output. `_check` always passes one, so `timeout_s` is ignored. Runs: `scheduler_silence.py` keeps state in `<state>/silence/N.json`. Do not put it under `runs/`, because that folder parses `issue-N.json` names as integers.
**Why:** #431's fork test needs fresh listings for stops, and #435 needs exactly one listing for a silence stop. `processes.quiescent_tree()` limits snapshot reuse to trees proven silent.
**How to apply:** 고정 제한을 가정하던 단위 시험(test_scheduler, test_executor_recovery, runner_support의 run_case)은 `max_runtime_minutes=90`을 명시하고, scheduler 계열은 `silence_minutes=24*60`도 명시해 stall·runtime 경로를 그대로 시험한다. 인수 시험의 가짜 git에는 `at`이 없어 제품 코드에서 getattr로 우회했다(`_checkout`, `follow_base`). 감시기 판정 기록은 정지가 성공한 뒤에만 남긴다(거부된 정지는 stop-refused만 남는다). zsh에서는 `$files`가 낱말로 나뉘지 않으므로 `${(f)...}` 배열을 쓴다. 단위 시험 묶음이 10분을 넘으면 나눠 돌린다.

2차 리뷰(6건) 반영: 실행기의 예산 `_remaining`은 안전망에 10분 여유(`RUNNER_MARGIN_SECONDS`)를 더한 값이다. 그래서 게이트의 안전망 정지(WIP 보존, ready 복귀)가 먼저 일어난다. `max_runtime_minutes`는 scheduler와 `past_runtime_limit()`(단계 경계에서, 직전 검사나 리뷰가 멈췄을 때만)에서만 쓴다. fa doctor는 `RunContext.deadline_seconds`를 쓴다. 침묵 정지와 안전망 정지는 항상 stalled-restart이고 재시작 예산을 쓰지 않는다. CPU를 읽지 못하면 침묵으로 보지 않는다(`Decision.cpu_known`). 침묵한 트리를 멈출 때는 `read_children()`로 늦게 생긴 자식도 찾는다. 기준선 통과 ID는 pytest 플러그인 `RESULTS_PLUGIN`이 기록한다.

3차: 대기 루프(CI 폴링 `run_publish` 126/150, `ci_mode` 227, merge turn `run_merge_turn`)는 범위 밖이므로 `_wait_remaining()`으로 예전 한도(`max_runtime_minutes`)를 유지한다. 이 함수는 `runner._remaining` seam을 거쳐 부른다. transient 실행기 실패도 진행 없는 단계로 보고, 한도를 넘었으면 `past_runtime_limit`로 timeout 처리한다. `test_orch.py`는 merge turn 대기가 24시간이면 10분 넘게 돈다(멈춘 것처럼 보임). `faulthandler_timeout`을 걸고 백그라운드 `-v`로 실행하면 어느 시험인지 찾을 수 있다.

4차(main 병합): main(#433)이 timeout된 캡처도 `<key>.json`에 timed_out=True로 기록한다. `load_acceptance_baseline`이 그런 기록을 None으로 돌려야 잠긴 침묵 시험이 통과한다. dream 실행(run.key=`dream-<lane>`)은 같은 정지 경로를 지나므로 error로 끝나고 라벨을 건드리지 않는다. 출력 로그는 `orch_dream.log_path`이고, `_kill_run`에 `decided=run`을 넘긴다. 크기 제한 때문에 run_baseline(600), run_checks(600), scheduler_recovery(300)를 줄였다. 플러그인과 재개 코드는 `run_baseline_plugins.py`로, 감시기 입력은 `watchdog.silence_args(ctx)`로 옮겼다. main 기준선 비교 방법: 작업공간 안 `.foragent/base`에 `git archive <main> src tests pyproject.toml`을 풀고 그 안에서 `PYTHONPATH=src ../../.venv/bin/python -m pytest`를 실행한다 (내 conftest의 read_children 패치를 피한다).
