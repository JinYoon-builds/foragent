# View lane 기억

## 시험 돌리기·기준본 비교
- 시험은 `fa test <파일...>`로 돌린다. Done-when과 바꾼 파일의 시험만 돌리고 전체는 돌리지 않는다(부하 때 관련 묶음만 6~17분). stop-hook 검사가 도는 동안 따로 `fa test`를 겹쳐 돌리지 않는다(같은 시험 자리·CPU를 다툼). 의심되는 실패는 그 파일만 단독으로 다시 돌린다.
- uv를 직접 써야 하면 `UV_CACHE_DIR=<저장소>/.uv-cache UV_OFFLINE=1`처럼 캐시를 저장소 안에 둔다. 목표에 "저장소 밖 변경 없음"이 있으면 `/tmp`를 쓰지 말고 TRY용 임시 파일도 `.foragent/` 안에 둔다(#116 때 `/tmp` 캐시로 조건을 어겼다).
- 기준본 비교: `git archive <기준 커밋> src`를 `.foragent/base`에 풀고 gitignore된 `src/foragent/_build.py`를 복사한 뒤 `PYTHONPATH=.foragent/base/src`로 같은 묶음을 돌려 실패 ID를 대조한다(새 실패 0 확인). 지난 회차 수정이 커밋되지 않았으면 `git checkout-index -a --prefix=.foragent/base/`로 스테이징 상태를 쓴다. 화면 불변 검증은 여러 너비·tick·언어의 `render_frame`과 layout hits를 양쪽에서 출력해 `cmp`한다.
- 회귀 시험은 고친 곳을 되돌려 실패하는지 확인한다. break-it 백업 폴더는 먼저 `mkdir -p`한다(cp 실패로 변형이 쌓임).
- 실제 TTY 확인: `pty.fork`로 자식에서 `watch.run`을 돌리고 키를 보내 출력에서 1049h/l·OSC 8·위치 줄을 찾는다. 창 크기는 TIOCSWINSZ + SIGWINCH. 실제 경로 회귀는 `watch.run` + Mock screen(getmaxyx)으로 addstr를 모은다.
- 일반 시험 파일은 인수 시험과 다른 basename을 쓴다(`tests/test_watch_split.py`는 `tests/acceptance/test_watch_split.py`와 충돌해 수집 실패). 옛 문구를 바꿀 때는 소비처를 `tests/` 전체에서 grep한다(예: `watch_open_failed`는 scheduler `view_opener` 시험도 씀).
- 시간 초과·GitHub 오류로 작업 트리가 다시 만들어지면 코드는 브랜치의 WIP 커밋에 남는다. 커밋되지 않는 `.foragent/` 파일(TRY·REBUTTAL·QUESTION·try 스크립트)만 다시 쓴다. 리뷰가 main 대비 "삭제"를 지적하면 기준 커밋이 main보다 뒤인지 먼저 본다.
- `.foragent/acceptance-exclusions.json`은 이제 추적하지 않는다(gitignore). 제외 목록은 러너가 커밋 trailer `Foragent-Acceptance-Excluded`(`ci_workflow.EXCLUSIONS_TRAILER`)로 남기고 `ci_mode.comparison`이 검사한다. 손대지 않는다. 리뷰가 "자기 시험을 뺐다"고 하면 REBUTTAL로 답한다.

## 고정 시험·리뷰와 부딪힐 때
- 러너는 base에서 통과하던 인수 시험을 모두 기준선으로 본다. 다른 이슈의 Done-when 시험이 main 병합으로 들어와 "기존 통과"에 섞이면 `git show <커밋> -- tests/acceptance/<파일>`과 `gh issue list --search`로 담당 스펙을 찾아 최소로 맞춘다.
- 고정 시험과 Goal 문장이 동시에 만족되지 않으면(셀 수 등으로 확인) 우회 구현을 만들지 말고 바로 `.foragent/QUESTION.md`로 올린다. 질문마다 A/B/C와 추천, Evidence에 이슈 문장·시험 행·기준본 행을 쓴다. 코드는 시험을 통과하는 쪽에 둔다(#167·#172·#503·#170 모두 이렇게 끝남).
- 오케스트레이터 결정이 이슈 본문에 없으면 리뷰는 "Goal과 모순"으로 계속 반려한다. REBUTTAL만으로는 끝나지 않으니 QUESTION.md로 본문 갱신을 요청한다. 이슈 본문은 직접 고치지 않는다(실행 중 해시 불일치). 본문이 고쳐지면 남은 QUESTION.md를 지운다(안 지우면 spec-question으로 다시 멈춤). 고친 스펙 문장을 그대로 구현하고 옛 우회 코드는 지운다.
- 고정 시험 충돌이 아닌데 같은 지적이 반박 뒤에도 돌아오면 다시 반박하지 말고 두 조건을 모두 지키는 설계로 바꾼다(#756 /fa 오프라인). 지적이 2~3회 반복되면 그 자리 땜질이 아니라 결함 종류 전체를 찾는다.
- 고정 가짜 함수 함정: `test_watch_split`·`test_watch_open`·`test_watch_reload_runtime`의 가짜 `render_frame(*args)`는 키워드를 받지 않고 `(snapshot, config, repo_root)`와 정확히 비교한다. `test_watch_compat`의 가짜 wrapper는 `lambda *args`라 `_terminal`·`_activity`에 새 인자는 위치 인자로 넘긴다. 바꿔야 할 신호는 contextvar로 넘긴다(예: `finished.calm()`).
- `test_watch_split`: `watch.py`가 `watch_` 아닌 새 foragent 모듈을 import하면 실패한다. `test_view_sections`: `watch_layout.py`에 화면 i18n 키 문자열이 없어야 하고, 구역 render 호출 횟수·width==120을 검사한다. `test_state_records_tolerant`: `finish_run`이 status·outcome·detail 외 Run 필드를 바꾸면 실패한다 → Run에 필드를 더하지 말고 `runs.jsonl` 기록 옆 키(`finished_at`·`merged_at`, `state_runs.finish_times()`·`merge_times()`)로 둔다.
- 화면 문구는 고정 시험끼리 갈린다: `watch_runner_gone`이 "○ ⚠ 작업자가 없어요"인 이유는 test_view_watch·test_stall_visibility·test_view_evidence를 동시에 맞추기 위해서다. 문구를 바꾸면 셋을 함께 돌린다.

## 리뷰가 반복해서 잡는 결함 종류 (view)
- 이슈 나열 누락: status 레인 행의 `issue`는 대표 하나뿐이다. 실행 없는 막힘·사람 기다림·대기 이슈도 줄이 있어야 한다 → `collect_lanes`의 `held`·`queued`를 모두 그린다(#167·#170·#477).
- 상태 판정 불일치: 줄 상태는 `status.issue_state(labels, active, group)` 하나로 정한다(needs-human·waiting-person → blocked → 실행 → pr-open → queued). 상자·작업자 줄·머리줄 집계가 같은 함수를 쓴다. "실행 끝남 ≠ 이슈 끝남"(closed일 때만 끝났어요).
- 잘린 기록을 원본으로 씀: status JSON `history`는 최근 10건뿐이다. 완료 집계·실행 시작은 `StateStore.read_history()` 전체나 `Snapshot.run_starts`·`run_phases`·`titles`(로컬 이슈 기록)를 쓴다.
- 덮어쓰는 스냅숏으로 "언제" 판단: flags·승인·라벨·runs/<n>.json으로 역사를 만들지 않는다. 덧붙이는 기록(`orch-events.jsonl`, `runs.jsonl`)만 읽고, 표본은 처음 본 사실로 한 번만 기록한다(D95; #503·#756).
- 너비: 뒤쪽 사실(도토리·시간·번호)이 앞 글에 밀려 잘린다 → 덜 중요한 칸(경고 → 레인 이름 → 단계 말)부터 줄인다. "줄바꿈" 요구 줄은 `_fit`으로 자르지 않는다. `_wrap`은 최소 1칸. 실제 터미널 폭은 `getmaxyx` 그대로 넘기고 다시 자르지 않는다.
- 높이(24줄 `SCREEN_ROWS`): 구역이 통째로 빠지면 KEEP/STAYS/TIGHT로 지킬 것을 정한다. 리뷰는 plain 문자열이 아니라 실제로 보이는 줄을 본다.
- 0을 없음으로 취급(시험 수 0/0, 요약), 빈 시간 칸(기록 없으면 `—`).
- 확인과 행동 사이 틈(TOCTOU): 리뷰는 확인-실행 사이마다 같은 종류를 찾으니 처음부터 명령마다 다시 확인한다(#543).
- 기능 문구는 공유 messages 파일(watch.py·squirrel.py)이 아니라 기능별 `messages/<기능>.py`에 새 키로 둔다.

## 화면 구조
- `watch.py`는 다시 내보내기와 `run`만. 수집 `watch_data.py`, 셀 너비·`_clean`·`_columns`·`_beside` `watch_cells.py`, 조립·`_frame`·`_window`·`_clones` `watch_layout.py`, curses·입력·갱신 `watch_terminal.py`, 키·마우스·스크롤 `watch_mouse.py`, 그림 `watch_art.py`. 분리된 함수는 함수 안에서 `from foragent import watch` 후 `watch.<이름>`으로 읽어야 교체가 반영된다(`tests/test_watch_compat.py`).
- 구역은 `watch_sections/<이름>.py`의 ORDER·render(자동 발견, 패키지 안에 도우미 모듈 금지 — 밑줄 이름도 발견됨). 현재 ORDER: squirrels 3, prep 4, lanes 5, acorns 6, stage 20, progress 21, finished 22, done 23, summary 24, orch_panel 35, activity 40, detail 50, help 60, gates 70. 속성: GAP(False면 앞 빈 줄 없음), GAP_AFTER, KEEP, STAYS, TIGHT. 상세: [#353](project_watch_sections.md), [#167](project_lane_rows.md).
- 구역은 state에 직접 쓰지 않고 hits·marks를 낸다. `_frame`이 마지막 조립 결과로 한 번만 state를 만든다(24줄 trimming이 다시 그려도 낡은 값이 안 남음).
- `watch._line`·`_wrap`은 `_clean`으로 공백을 접어 들여쓰기가 사라진다. 들여쓴 줄·원문 표시는 `[(text, style)]`로 직접 만든다. 한글·이모지는 터미널 셀 너비(🐿은 `WIDE_EMOJI`로 2칸).
- 색은 `watch_terminal.color_pairs(colors)`. 배경은 모두 -1(테마 따름). 스타일 키는 `yellow`·`claude`(자홍)·`divider`(청록) 등 기존 것만 있다.
- OSC 8 등 curses 밖 쓰기는 refresh 뒤 `\x1b7`…`\x1b8`로 감싸고, 캐시 없이 refresh마다 쓴다(curses가 크기 변경 때 같은 글자를 다시 그림).
- 문구 원본은 `messages/<기능>.py`의 `MESSAGES`(en·ko), `i18n.py`에는 dict를 두지 않는다. 새 키 이름은 먼저 grep한다(`watch_waiting` 등 이미 쓰임). 고정 시험이 `text()`로 읽는 키는 지우지 않는다. 문체는 `docs/voice.md`(한국어 해요체); `{time}` 자리에는 `—`도 들어가니 세 경우 모두 자연스러운 라벨을 쓴다.
- 레인 사람 이름은 `labels.lane_display(config, slug, language)`. JSON의 slug는 유지한다.
- 상태·레인 화면은 로컬 기록만 읽는다(`StateStore.local_issues()`·`issues_observation()`); 부모·의존·PR·댓글은 `RecordingTracker.refresh_issues`가 기록한다. 병합 PR을 먼저, 열린 PR을 나중에 적용한다. 일반 시험에서 FakeTracker로 상태를 만들려면 먼저 `refresh_issues()`.
- 멈춤·증거: `min(10, stall_minutes)`분, 작업자 활동과 runner `last_progress_at` 중 새 것; `start_run`의 시작 시각과 같은 progress는 무시. 증거 나이는 `watch_data.evidence`(heartbeat·로그 mtime). 상세: [#477](project_worker_rows_evidence.md).
- 끝나는 시각(#503): `progress.py`의 `merged_work` → `learned`(크기별 최근 30개·14일) → `expected` → `estimate`(합÷`parallel_budget`). 병합 사실은 `state_refresh._record_merge`가 PR을 처음 볼 때만 기록한다(이슈당 가장 최근 병합 하나, `closed`는 updated_at ≥ merged_at인 첫 관찰에서 정함). 시간 창으로 gate PR을 가려내지 않는다.
- 쿼터(#169): 로그의 rate-limit 사건을 덧붙은 부분만 읽고, 구간 이름마다 따로 이벤트 시각(없으면 리셋 시각, 사용률)으로 가장 새 것을 고른다.
- 상호작용·스크롤·상자: [#170](project_acorn_interact.md). 불꽃: [#168](project_finished_fireworks.md). 준비 줄: [#171](project_prep_rows.md). 오케 상자: [#172](project_orch_panel.md). 완료 구역: [#114](project_done_state.md).

## 화면 열기·재실행
- 열기는 `watch_open.open_view(repo_root, store, language)`, 자동 열기는 모두 `commands._open_lane_view(root)`를 거친다(`FORAGENT_VIEW=off`·OSError 격리). 순서: Orca → tmux → WezTerm → Kitty → iTerm2 → Terminal.app; WezTerm·Kitty는 argv 목록. 열 도구가 없으면 수동 명령 한 줄만 출력(시도가 아님). gate 재열기는 `watch_open.reopen`(5분 간격, `watch-open.json`).
- 화면 하나만: `watch.lock` flock, 열기 직렬화 `watch-open.lock`, execv 때 `FORAGENT_WATCH_LOCK_FD`로 잠금 인계. 상세: [#543](project_single_lane_view.md).
- 시험의 autouse `no_view_windows`가 `FORAGENT_VIEW=off`를 건다. 열기 시험만 해제하고 가짜 명령을 쓴다(진짜 osascript는 SIGKILL 금지에 걸림). `watch_open.datetime`을 가짜 시계로 바꾸므로 모듈 이름으로 부른다.
- 빌드 재실행: 5초 갱신 때 `_installed_build`가 `_build.py`를 `runpy.run_path`로 다시 읽고, 재실행 요청은 curses 콜백 반환값으로 올려 `watch.run`이 정리 뒤 `os.execv`한다. 실행기는 `ExitStack`에 넣고 재시작 때 `pop_all()`(with 블록은 멈춘 갱신을 join함). `watch._frame`의 ImportError는 재실행 사유.

## 그 밖의 명령
- `fa` 명령은 `commands/<이름>.py`(ORDER·add·run). 밑줄로 시작하는 모듈은 그룹 도우미로 발견에서 빠진다(옛 "도우미 모듈 금지"는 낡음). 핸들러는 `cli = facade()`로 `cli.<이름>`을 읽는다. 상세: [#429](project_cli_commands.md).
- 알림은 `notify.send(config, title, line, link)`(실패는 False, 성공 때만 서명 저장). inspect는 이미 `notify_lock`을 잡으므로 outbox는 파일 자체를 잠근다. 즉시 푸시 중복 방지는 `Item.notification_id`+`InspectState.notified_items`. Config 키를 더하면 `tests/test_config.py::test_exact_defaults`도 갱신.
- `fa brief`: `brief.collect(store, config, now)`, star 질문은 `star.py`(배타 생성·flock). /fa 시작 명령은 GitHub를 부르지 않는다: [#756](project_fa_offline.md), 보류·해제 출처: [#756](project_hold_sources.md).
- `fa --version`은 생성 파일 `foragent._build.GIT_SHA`(없으면 unknown), 실행 중 Git 호출 없음.
