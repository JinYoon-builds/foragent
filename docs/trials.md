# 시험 실행 기록

측정값: 사람 개입 횟수(실행 중 사람이 손댄 횟수), 재작업 PR 비율(머지 후 다시 손봐야 했던 PR), 예상과 다르게 갈라진 경로.

## Trial 1 — 2026-10-20, P3 커밋 직전 코드, 수동 tick 반복(30초)

시험 저장소 `JinYoon-builds/foragent-sandbox`, 이슈 4개 손으로 작성, `review: fast`, 실행기 codex.

| 이슈 | 의도한 경로 | 결과 | 시도 | Codex 세션 |
|---|---|---|---|---|
| #1 words | 정상 머지 | merged (PR #5, CI 성공 후 머지) | 1 | 26초 |
| #2 top N (blocked-by #1) | #1 뒤 자동 시작 | merged (PR #6) | 1 | 32초 |
| #3 report (스키마 없음) | spec-question | spec-question, 질문·선택지·추천 정확 | 1 | 23초 |
| #4 chars (시험과 Goal 충돌) | acceptance-touched | **spec-question** — 시험을 고치지 않고 충돌을 정확히 짚어 질문 | 1 | 23초 |

- 전체 약 5분(첫 시작 05:15 → 큐 비움 05:20). 대부분 CI 대기.
- 사람 개입: 0. 사람 결정 필요: 2 (설계상 의도).
- 재작업 PR: 0/2.
- 다르게 갈라진 경로: #4. 모델이 정직하게 물어서 acceptance-touched 잠금은 실제로 시험되지 않았다(가짜 실행기 테스트로만 확인).
- 발견한 문제:
  - gate 자신의 `VIRTUAL_ENV`가 Done when 명령에 새어 uv 경고가 난다 → 실행기·검사 환경에서 가상환경 변수를 지운다.
  - 검사 루프 스크립트 실수(상태 JSON 키) — 제품 문제 아님.

## Trial 2 — 2026-10-20, 리뷰 수정 반영 후, 수동 tick 반복(30초)

| 이슈 | 결과 | 시도 |
|---|---|---|
| #1 | merged (PR #7) | 1 |
| #2 | merged (PR #8) | 1 |
| #3 | spec-question | 1 |
| #4 | spec-question | 1 |

- 전체 약 6분. 사람 개입 0. 재작업 PR 0/2. 1회차와 같은 갈래.
- 가상환경 변수 누출 해결 확인(로그에 VIRTUAL_ENV 경고 0).
- 발견: 머지 후 GitHub가 이슈를 닫기까지의 몇 초 사이에 tick이 #1을 다시 ready로 올렸다(곧 닫혀 피해 없음). 같은 이슈 재실행 위험 → runner가 머지 직후 이슈를 직접 닫고, scheduler는 merged 이력 뒤 새 승인이 없으면 다시 시작하지 않게 수정.

## Trial 3 — 2026-10-20, launchd 자동 실행(5분 간격), 사람 손 없음

`fa init`으로 이슈 템플릿을 깔고, `fa gate install`로 launchd 등록. 승인 라벨만 붙이고 tick은 손대지 않음.

| 이슈 | 결과 | 시도 | 시작 |
|---|---|---|---|
| #1 | merged | 1 | 14:52 |
| #2 | merged | 1 | 14:57 |
| #3 | spec-question | 1 | 15:02 |
| #4 | spec-question | 1 | 15:07 |

- 전체 약 17분(5분 tick 간격이 대부분). 사람 개입 0. 재작업 PR 0/2. launchd 오류 로그 비어 있음.
- 2회차에서 고친 머지 직후 재승격 문제 재발 없음.
- 세 번 연속 같은 갈래로 끝남. 다음 측정은 P6(inspect) 이후 사람의 답을 받아 #3·#4를 끝내는 고리까지.

## inspect 실연 준비 — 2026-10-20
- Trial 3의 결과(#3·#4 spec-question, #1·#2 merged)를 `fa inspect`로 읽으면 "Decision 2 · Try 2". macOS 알림 한 줄 확인, 같은 내용 두 번째 호출은 조용함(중복 방지).
- `fa`를 `uv tool install --editable`로 PATH에 설치, 스킬 `fa-init`·`fa-inspect`를 `~/.claude/skills`에 링크.

## Trial 4 — 2026-10-20, P7(적대적 리뷰 단계) 적용, 수동 tick 반복
- #1, #2만 승인(#3·#4는 order 대기). 구현 codex, 리뷰 claude(fail-closed).
- #1 merged, #2 merged. 둘 다 "Round 1: VERDICT: PASS". 이슈당 약 2분(리뷰 포함).
- 사람 개입 0. 리뷰어가 FAIL을 내는 경로는 아직 실제로 시험하지 못함(가짜 실행기 테스트로만 확인).

## Trial 5a — 2026-10-20, P7b+P7c: codex 구현, /code-review 리뷰, language: ko
- #1, #2 merged. 리뷰 "Round 1: VERDICT: PASS / reviewer: /code-review low".
- TRY.md가 한국어로 생성되어 PR "Try it" 절에 들어감. 커밋에는 포함되지 않음(변경 파일 cli.py만).
- `fa inspect prepare` → 미리 실행 결과 `4`, `0`(기대값과 일치). 다듬을 점: uv 설치 메시지가 출력에 섞임 → stdout만 보여 주고 stderr는 실패 때만.

## Trial 5b — 2026-10-20, claude 구현(순정 /goal + deny + Stop 훅), codex 리뷰
- #1, #2 merged. 로그에 "Goal set: Every command in the Done when…", Stop 훅 1회 실행, 차단 0회(처음부터 통과).
- 수정 금지 규칙 별도 시험: 시험 파일 수정을 직접 시키자 편집 도구가 deny에 막힘, "다른 방법으로라도" 지시에도 우회하지 않고 스펙(글자 vs 바이트)을 되물음. 파일 변경 없음. 셸 우회는 규칙이 완전히 막지 못하므로 runner의 경로 잠금 판정을 계속 유지.
- 사람 개입 0.

## Trial 6 — 2026-10-20, P7d: 레인 2개 병렬, 같은 파일(cli.py)을 고치는 독립 이슈 #17(lane:lines)·#18(lane:stats)
- 1차: #17 merged. #18은 fresh-base 병합 충돌이 3회 반복되어 retries-exceeded. 원인: 충돌 시 merge를 abort한 뒤 구현자에게 문구만 줘서, 구현자 worktree가 옛 base라 해결할 충돌이 없었음 → 실제 결함 발견.
- 수정: 충돌 표시를 worktree에 남긴 채 구현자에게 "두 기능을 모두 살려 충돌을 풀라"고 맡기고, runner가 표시 잔존 확인 → 병합 커밋 → Done when 재검증 → 머지.
- 2차(동시 재실행): #17 merged(시도 1), #18 충돌 → 해결 → merged(시도 2). main에서 E·F 인수 시험 모두 통과. 전체 약 4분, 사람 개입 0.
- Orca: 시험 저장소를 Orca에 등록하고 외부 worktree 보이기를 켜서, 실행 중 `foragent/issue-17`·`issue-18`이 사이드바에 나타났다 사라짐.
