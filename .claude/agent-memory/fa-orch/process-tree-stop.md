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
- 실행이 두 번 `stalled`로 멈췄다. 출력 없이 오래 도는 전체 `uv run pytest`(백그라운드 포함, 약 50분)가 원인이었다. Done when과 변경 파일의 시험만 포그라운드로 돌린다. 전체 시험은 CI가 맡는다. `.foragent/TRY.md`는 작업공간 재생성 때 사라지므로 다시 쓴다.
- 리뷰 지적: 스냅샷 뒤 SIGTERM 처리기가 띄운 setsid 자식이 남았다. macOS는 `PROC_PIDT_BSDINFOWITHUNIQID`(18, 192바이트)의 `p_puniqueid`를 재부모화 뒤에도 보존하므로 이 연결로 추적한다. macOS는 `/bin/sleep` 같은 플랫폼 실행 파일의 환경 변수를 KERN_PROCARGS2로 보여 주지 않아 환경 표식(`FORAGENT_PROCESS_TREE`)은 Linux에서만 쓴다.
- 러너는 종료 중 `kick_tick()`으로 `gate tick`을 띄운다. 트리 종료와 탐색에서 `gate tick`·`gate run-issue` 프로세스를 빼고 그 아래로 내려가지 않는다. 자기 lineage도 탐색에서 제외한다.
- 고아 정리는 `tick()`에서 GitHub 한도 대기 두 경로(기록된 대기·도중 한도 오류)에도 실행한다(로컬 작업).
- 리뷰가 runner.py·tests/acceptance 변경을 지적하면 먼저 `git log HEAD..origin/main`을 본다. 병합 뒤 main이 앞서 나가면 diff가 되돌린 것처럼 보인다. REBUTTAL로 답한다.
