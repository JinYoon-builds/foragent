# View lane 기억

## 작업 환경·검증
- uv 캐시는 처음부터 저장소 안에 둔다: 첫 `fa test`/`uv run` 전에 `export UV_CACHE_DIR=$PWD/.uv-cache UV_OFFLINE=1`(`.uv-cache`는 gitignore). `fa test`는 기본 `~/.cache/uv`를 쓰고, 새 워크트리는 .venv가 없어 첫 실행이 editable foragent를 거기 빌드한다 — "저장소 밖 파일 없음" 목표가 되돌릴 수 없게 깨진다(#116·#173·#1190). `/tmp`에 파이프·임시 파일도 금지, pytest `tmp_path`용으로 `TMPDIR=$PWD/.uv-cache/tmp`. 끝나면 `.uv-cache`·`.foragent/base`를 지운다. 워크트리를 다시 만든 뒤 .venv에 pytest가 없으면 `UV_OFFLINE` 없이 한 번 채운다.
- 전체 `pytest tests`는 부하에서 10분+ 걸려 시간 초과된다. Done when + 바꾼 파일의 시험만 돌린다. 레인이 돌 때 전체 묶음 금지.
- 기준본 비교: `git archive <기준 커밋> src`(+gitignore된 `_build.py`)를 `.foragent/base`에 풀고 `PYTHONPATH=.foragent/base/src`로 같은 파일 목록을 돌려 실패 ID를 `comm`으로 대조한다. `fa test`는 cwd와 무관하게 워크트리 코드를 쓰므로 기준본엔 `<worktree>/.venv/bin/python -m pytest`를 쓴다. 지난 회차 수정이 미커밋이면 `git checkout-index -a --prefix=.foragent/base/`. `.foragent/`는 gitignore 대상이 아니니 꼭 지운다. 긴 기준선 비교는 Bash timeout ~10분.
- zsh: 변수 하나에 든 목록은 쪼개지지 않아 가짜 경로 하나로 돌고 FAILED 줄이 안 나온다 — `${=T}`(glob이면 `${~=T}`). 새 모듈을 import하는 새 시험 파일은 기준본에서 수집 ERROR로 전체를 멈추므로 목록에서 `grep -v`로 뺀다(`--ignore`는 명시한 경로를 못 뺀다). TRY용 `python -`에선 `foragent.cli`를 먼저 import한다(status→scheduler_stop 순환).
- 러너가 워크트리를 다시 만들면 커밋(WIP 포함)은 남고 `.foragent/TRY.md`·`REBUTTAL.md`·`.uv-cache`가 사라진다 — `git log` 확인 뒤 그 둘만 다시 쓴다. 스크래치 백업은 `.git/`(워크트리에선 파일)이 아니라 존재하는 `.foragent/` 폴더에 두고 `test -s`로 확인한 뒤 변형한다.
- `.foragent/acceptance-exclusions.json`은 러너가 섞어 넣는다. PR이 건드리면 CI 약화로 지적되니 main과 같게 둔다. `git checkout --ours`는 막혀 있어 `git show <rev>:<path> > <path>`로 대신한다.
- Done when이 전체 묶음을 요구하는데 기준본에서도 형제 이슈 소유 시험이 실패하면 구현으로 못 푼다. 일찍 QUESTION.md(추천: 지정 시험+새 실패 0)로 올린다(#1058·#1059, 오케가 받아들임).
- 레인 세션은 `tests/acceptance/` 편집이 권한으로 막힌다(D11). 스펙이 "이 이슈가 고정 시험에 패치를 넣어라"라고 하면 실행 첫머리에 QUESTION.md로 올린다(#1062는 막판에 발견해 한 바퀴를 잃었다).
- 기준선 회귀가 다른 레인 시험이면 먼저 시험 주인 쪽 수정이 main에 있는지 `git log HEAD..origin/main`으로 본다. `runs/issue-*-check-*.log`에서 실제 오류부터 읽는다. `test_facts_baseline_trend_..._read_only`는 부하에서 간헐 실패(#173·#308·#1014·#1026) — 재실행하고 REBUTTAL, facts 코드는 건드리지 않는다.
- `tests/acceptance/test_question_recipient.py`는 기준본에서도 20/20 실패(#1160 소유), `test_settings_window.py::..._footer_reflects_review_changes`도 기준본 실패.

## 리뷰가 반복해 잡는 결함 종류
- 맞추기(fitting) 중 필수 내용 손실 — #174·#1062·#1160이 모두 여러 번 걸렸다: 좁은 폭에서 💬 표시·받는 사람 이름·레인 이름 `…`·도토리 번호 전체가 잘리거나, 높이 1~2·접힌 화면에서 결과/오류 줄이 그려지지 않는다. 필수 표시는 줄일 수 있는 칸 밖에 두고, 줄이면 `…`를 붙이고, 넓이 4종(10·30·40·160)과 높이 1~3, en/ko, 작업자 여럿, ASCII/animation=False를 한 번에 시험한다.
- 표시 경로가 투영(projection)을 우회 — 같은 사실을 보여 주는 모든 화면(레인 줄·이슈 상자·머리줄·statusline·presence 접미사)은 한 수집 함수의 결과만 읽는다. 하나라도 원시 기록을 직접 세면 화면끼리 모순된다(#1160 상자·presence.py).
- 질문 정체성: 답은 그 질문(id·실행)에만 묶는다. 이슈 번호·등록 순서·다른 종류(merge/order) 답·이전 실행의 답이 새 질문을 ANSWERED로 만들면 안 되고, 재시도·병합 뒤 옛 질문은 사라져야 한다(#1160 4~8회차).
- 라벨에서 사실을 파생 — 대기 사실·보류 사유는 기록(승인 스냅숏 `store.load_approval`, `blocked_by`, journal, budget-history)에서 온다. 라벨은 표시용이며 스펙이 이름 붙인 경우만 예외(#1060). 게이트 정책을 표본에 다시 돌려 사유를 만들지 말고 기록된 행을 읽는다.
- 선택적 읽기는 없음으로 읽히고 화면을 죽이지 않는다: OSError/ValueError → None, 읽을 수 없는 기록은 그 기록에 기댄 사유를 끈다(#1060). 손상 행은 행마다 격리, `Exception`으로 잡기(RecursionError), JSONL은 `\n`으로만 자르고 파싱한 필드로만 거른다(#1026, [prompt_context](project_prompt_context.md)).
- 저장 안 된 사실을 확인으로 처리 — 전달 표시는 실제로 저장(push 또는 journal 기록)이 성공한 값으로만 남긴다(#1030).
- 상태가 caller 소유 state에만 있음 — `render_frame(height=)`처럼 state 없는 경로도 같은 결과를 내야 한다. 그림용으로 고친 지도는 키 입력이 읽는 곳에도 되쓴다(#1061).
- 대기·보류를 실행 중으로 읽음 — 실행 중은 레인 줄 `state == "working"`(status.issue_state) 또는 비지 않은 `running`. `_has_work`·`active_runs`·`summary.waiting_human`은 다른 것을 센다(#1061).
- 로컬 사본이 공개 사실을 대신 — 병합된 PR의 try 카드는 PR 본문 `## Try it`만 읽는다. 없으면 카드 없음, `<state>/try/<n>.md`로 대체하지 않는다(#1190).
- 같은 결함 종류로 두 번 보류되면 땜질 말고 재설계한다(D106). 고정 시험과 스펙이 충돌하고 오케가 정했으면 우회 구현 대신 처음부터 REBUTTAL; 오케가 스펙을 고치면 고친 문장대로 구현하고 옛 우회 코드는 지운다.

## 구조
- `watch.py`는 이름을 다시 내보내고 `run`만 둔다. 수집 `watch_data`, 셀 `watch_cells`, 배치 `watch_layout`, curses `watch_terminal`, 입력 `watch_mouse`, 뷰포트 `watch_viewport`, 그림 `watch_art`. 분리 함수는 함수 안에서 `from foragent import watch` 후 `watch.<이름>`으로 읽어야 패치가 먹는다. `test_watch_split`은 `watch.py`가 `watch_` 아닌 모듈을 import하면 실패하고, `render_frame`은 `_frame`에 tick/width/selected/now(+layout)만 넘겨야 한다. `_terminal`·`_activity` 새 인자는 위치 인자로(compat 가짜가 kwargs를 안 받음).
- 일반 시험 파일 basename은 인수 시험과 달라야 한다(같으면 수집 실패).
- 화면 구역: `watch_sections/*.py`는 모두 구역(ORDER·GAP·render). 도우미는 패키지 밖 `watch_*.py`, 창·오버레이는 패키지(발견이 ispkg를 건너뜀) — [구역 모듈](project_watch_sections.md), [설정 창](project_settings_window.md).
- #1061 셸: `lane_header`(ORDER 2, PIN header, squirrels/summary 대체), `lane_footer`(ORDER 100, PIN footer, usage/help 대체, `usage._parts(include_capacity=False)`). `_frame`은 기본 높이 없음(KEEP 빼기·STAYS 이동 삭제, TIGHT는 명시 높이 넘칠 때만). 고정 프레임은 `watch_viewport.window`가 `body_length`·`body_height`·`row_map`을 state와 layout에 쓰고 hits를 줄마다 옮긴다. `scroll_limit`·`highlighted_row`·도토리 클릭·강조는 그 지도를 읽는다. `_frame`은 `layout['pinned_rows']`도 쓴다. 4줄 미만이면 `state['row_map']`을 지워 F의 첫 이슈 줄을 강조. 옛 24줄 예산·KEEP 이야기([레인 줄](project_lane_rows.md)·[사용량 줄](project_usage_line.md)·[꿈 줄](project_dream_row.md)·[도토리](project_acorn_interact.md)의 F 규칙)는 이 변경 전 기준이다.
- #1061 터미널: `timeout(500)`, `tick=int(elapsed/0.5)`, 데이터 기한은 시작+5k를 `while`로 넘긴다(놓친 건 건너뜀, 완료 때 리셋 안 함). 설정 저장은 `refresh_now`. `erase()` 한 번 뒤 줄마다 `clrtoeol`, 아래는 `clrtobot`. 가짜 화면은 `move`/`clrtoeol`/`clrtobot`이 있어야 하고 `refresh()`로 프레임을 나눈다. 옛 단위 잠금 34개는 이름을 유지한 채 단언만 고쳤다(footer `[-2]` 사용량·`[-1]` 도움말, 클릭은 `layout['row_map']`).
- #1058 m2 골격: [m2 skeleton](project_m2_skeleton.md) — REPLACES/PIN, `_Palette` 지연 색 쌍(8 이상은 `_Palette.free` 확인, `COLOR_PAIRS`는 `start_color()` 뒤에만 존재). 실제 터미널은 `COLOR_PAIRS=256`이고 테마 고정 시험 mock엔 없다 — 큰 그림이 색 쌍 8번 이상을 등록하면 정확 일치 기록기가 깨지니 `COLOR_PAIRS=256`으로도 돌려 본다(#1062).
- #1059 그림: `watch_art.ansi_rows`는 아래만 있는 칸을 `▄` + `38;5;<lower>`로 그린다(`half_blocks`는 `("▀", None, lower)` 그대로). 애니메이션은 비공개 4프레임 튜플을 `tick % 4`로 — [m2 art](project_m2_art.md).
- 색: `watch_terminal.color_pairs(colors)`, 배경 모두 `-1`. OSC 8 링크는 refresh 뒤 `\x1b7…\x1b8`로 감싸고 매 refresh 다시 쓴다(크기 변경 뒤 사라짐).
- 문구: `src/foragent/messages/<기능>.py`의 `MESSAGES`(en·ko), `i18n.py`엔 dict 금지. 공유 파일 대신 기능 파일에 새 키. 자리표시자 `{key}` 금지. 바꾸기 전 키 이름과 옛 문구를 `tests/` 전체에서 grep(scheduler `view_opener` 등). 문체는 `docs/voice.md`. `{time}`에 `—`가 들어올 수 있다.
- 설정 창은 디스크에서 config를 다시 읽으므로(`_fresh`) 그 config에서 읽은 문구는 파일 언어를 따른다 — 뷰 언어를 명시로 넘긴다(#1061).
- 새 Setting: `settings.SETTINGS` 한 줄 + `settings_desc_<key>`·`settings_window_label_<key>`·`settings_window_value_<key>_<value>` 문구 + 개수 시험 갱신(`tests/test_settings.py` 2개, `tests/test_watch_settings.py`, `tests/test_config.py::test_exact_defaults`). 키 고유 화면 로직은 `watch_sections/settings/<key>.py` — [runner 설정](project_settings_runner.md).
- `_line`·`_wrap`은 연속 공백을 합친다. 정렬 줄은 raw span `[(row, style)]`, 원문은 `acorns._verbatim_wrap`.
- 고정 `test_state_records_tolerant`: `finish_run`이 Run 필드를 바꾸면 실패 — 새 값은 `runs.jsonl` 옆 키(`finish_times()`·`merge_times()`)로.

## 상태·사실
- `status.collect`는 로컬 기록(`local_issues()`·`issues_observation()`)만 읽는다. 사실은 `RecordingTracker.refresh_issues`가 기록(병합 PR 먼저, 열린 PR 나중). 줄 상태는 `status.issue_state(labels, active, group)` 하나. status JSON `history`는 10건뿐 — 집계 원본은 `StateStore.read_history()`.
- #1060 레인 사실: `watch_lane_facts.py`(lane/waiting/park, `merged_today`, journal 한 번 읽어 공유)와 `watch_role_facts.py`(architect/docs, `orch_architect.read_record`/`session_alive`를 모듈 경유로 불러 패치 가능). 대기 종류 순서: 열린 의존 → 기록된 단계 지도 → 실제 겹침(repo_root 있을 때, `overlap_files.admits(ctx, lane, parsed, peers, [], union=recorded_union)`로 게이트 규칙 전체 재사용, 교집합은 한도와 무관하게 보고) → `machine`(`<store.root.parent>/budget-history.jsonl`의 최신 `rule: memory-critical` 행, 뒤의 실행 시작이나 `pressure: normal`로 끝남; 부하는 최신 `parallel` 감소 행 `load_per_cpu >= OVERLOAD_LOAD`; `config.max_parallel`이 있으면 부하로는 machine 아님) → `capacity`. 비교하는 시각은 aware 전용 `_time`을 거친다. 병합의 실행은 `merged_work`가 보는 기록(history + status `active_runs`, (번호, aware started_at)).
- ETA·병합 표본: [progress](project_watch_sections.md)의 `merged_work`→`learned`→`estimate`. 표본은 병합을 처음 볼 때 사실로 한 번만 기록(`state_refresh._record_merge`), 이후 갱신 금지. gate PR을 시간 창으로 가려내지 않는다.
- 보류·해제는 journal 사건만 — [hold sources](project_hold_sources.md). 오케 메모 줄 — [orch panel](project_orch_panel.md). 작업자 줄·증거 나이 — [worker rows](project_worker_rows_evidence.md). 준비 줄 — [prep](project_prep_rows.md). 완료 구역·불꽃 — [done](project_done_state.md), [fireworks](project_finished_fireworks.md).

## 알림·inspect
- #1030 알림 종류는 `notify.sending(kind, root)`(ContextVar)로 전달한다. 고정 가짜가 `send(config, title, line, link=None)`이라 새 키워드를 넣으면 깨진다. `send`는 bool, `deliver(..., kind, root)`는 오류 문자열. `notify_filter`(me 범위, 개인 값이 `.foragent.yml`보다 우선)가 정하고, 푸시 안 한 알림·실패는 `orch_inbox.record_notice` journal 소식이 된다.
- #1030 `deliver`가 푸시하지 않은 알림을 성공으로 보고하는 건 `_notice`가 None(journal 기록 성공)일 때뿐이다. `orch_silent.check`·`orch_push.after_tick`·`inspect_actions.notify`가 그 값으로 전달 표시를 저장. `notify: none` 무동작은 결정 종류와 저장소를 모를 때만. 회귀: `tests/test_notify.py -k not_acknowledged`.
- #1030 origin은 생산자의 root에서 `GitRepo(folder).remote_slug()`로 늦게 구한다(include.path·insteadOf·linked worktree를 git이 처리; cwd·`origin_slug_from_files` 금지). `command:` 대상에선 git을 부르지 않는다(`subprocess.run` 패치 시험). 시험은 `git commit` 불가라 linked worktree를 손으로 만든다(gitdir+commondir+HEAD). "origin 없음"은 원격 없는 `git init`(워크트리 안 TRY 폴더는 워크트리 `.git`을 찾는다). `orch_push`에 `ctx.repo`를 넘기면 scheduler 시험 49개가 흔들린다.
- inspect 즉시 푸시·중복 방지는 `Item.notification_id` + `InspectState.notified_items`, 아침 요약은 `summary_date`(#250). `notify.send` 실패는 False, 성공 때만 기록 → 재시도 가능.
- try 카드: 위 "로컬 사본" 규칙(#1190). 회귀: `tests/test_inspect_runtime.py::test_store_card_precedes_pr_fallback_and_missing_card_is_optional`(이름 유지) + `tests/acceptance/test_ssot_published_try.py`.

## 화면 열기·재시작
- `watch_open.open_view(repo_root, store, language)` 순서: Orca → tmux → WezTerm → Kitty → iTerm2 → Terminal.app(argv 목록은 그대로, 셸은 `shlex.join`, AppleScript는 `json.dumps(ensure_ascii=False)`). 자동 열기는 모두 `cli._open_lane_view(root)`를 거친다(`FORAGENT_VIEW=off`·OSError 격리). gate 재열기 `watch_open.reopen`(5분 간격). 화면 하나만: [single lane view](project_single_lane_view.md). `_running`은 psutil create_time과 watch.pid mtime으로 PID 재사용을 가린다(#942). watch-terminal.json 기록은 `watch_open.record_terminal` 하나(#951).
- 빌드 감시·재실행: `_installed_build`는 `runpy.run_path`로 다시 읽고, `watch.run`이 curses 정리 뒤 `os.execv`. 실행기는 `ExitStack`에 넣어 재시작 때 `pop_all()`(#191·#475). launchd 재불러오기 — [launchd](project_launchd_reload.md).
- 명령 묶음 — [cli commands](project_cli_commands.md). 기획 차례 상자 소식·프롬프트 훅 — [prompt context](project_prompt_context.md). `fa brief`·star 질문: `brief.collect`가 `status.collect` 재사용, 노출 기록은 `os.link` 배타 생성, 답은 flock 안에서(#113·#418). /fa는 GitHub 호출 없음 — [fa offline](project_fa_offline.md).
