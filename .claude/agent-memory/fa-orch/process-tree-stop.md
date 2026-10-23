---
name: process-tree-stop
description: #261 프로세스 트리 종료·작업공간 고아 정리의 함정 — 모의 waitpid와 subprocess, SIGSTOP 순서, macOS EPERM, lsof 부재
metadata:
  type: project
---

#261에서 `kill_process_group()`은 신호 전에 그룹을 벗어난 자손을 스냅샷하고, tick은 `_reap_orphans()`로 실행 중이 아닌 `worktrees/issue-N`의 프로세스를 cwd·argv 경로로 찾아 멈춘다(`orphans-reaped` 이벤트).

- 종료 경로에서 `ps` 같은 subprocess를 쓰면 안 된다. `tests/test_executors.py`가 `foragent.processes.os.waitpid`·`time.sleep`(전역 time 모듈)을 모의하므로 subprocess 대기가 무한 루프가 된다. 그래서 프로세스 표는 macOS libproc(`proc_listallpids`, `PROC_PIDTBSDINFO`, `PROC_PIDVNODEPATHINFO`, sysctl `KERN_PROCARGS2`)과 Linux `/proc`에서 직접 읽는다.
- 이 맥미니에는 `lsof`가 없다. cwd는 libproc으로 읽는다.
- 벗어난 자손에 SIGTERM을 먼저 보내면 실행기가 정상 종료한 것처럼 보여 run 결과가 `error`가 된다. 먼저 SIGSTOP, 그룹 종료 뒤 SIGKILL.
- macOS는 종료 중인 그룹의 `killpg(pid, 0)`에 EPERM을 준다. 확인 루프에서 "아직 남음"으로 처리해야 `test_tick_cleans_worktree_when_real_runner_is_terminated_between_logged_commands[True]`의 간헐 실패가 사라진다(기준선에서도 4번 중 1번 재현).
- `kill_process_group`의 그룹 신호 순서(SIGTERM → sleep(10) → killpg 0 → SIGKILL)는 기존 단위 시험이 고정한다. 바꾸지 말 것.
- `tests/acceptance/test_scheduler_split.py`(scheduler.py 250줄 제한)는 다른 이슈의 시험이라 기준선에서도 실패한다.

**Why:** 같은 영역(#243 시험 수 제한, #221)이 이어서 이 코드를 건드린다.
**How to apply:** processes.py·scheduler 종료 경로를 고칠 때 위 제약을 먼저 확인한다.
