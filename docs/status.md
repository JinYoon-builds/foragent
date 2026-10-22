# 설계 현황

마지막 갱신: 2026-10-20

| 부분 | 상태 | 설계 위치 | 남은 것 |
|---|---|---|---|
| 제품 정의·이름 | 확정 | [decisions.md](decisions.md) D1, D2 | — |
| 큰 구조 | 확정 | decisions.md D3–D5 | — |
| order ↔ gate 계약 | 합의 | [contract.md](contract.md) | 승인 신원(Q1) |
| `init` | 범위만 정함 | contract.md 4번 | 질문 목록, 기본 CLAUDE.md, 스킬 고르기 |
| `order` | 초안 | [order.md](order.md) | 일괄 승인 방식(Q1) |
| `gate` | 설계 완료(v1) | [gate.md](gate.md) | 빌릴 코드 줄 확인, Symphony SPEC 대조, 멈춤 신호 구체화 (gate.md 12장) |
| `inspect` (구 morning) | 초안 | [inspect.md](inspect.md) | 실제로 써 보며 조정 |
| 시험 | 미착수 | — | 이슈 3–4개짜리 시험 프로젝트 |

## 재료

- 옛 delion 스킬: delion-plan(분해 기준), delion-lane(구현 규칙), delion-qa(검증 절차), delion-wave(Ouroboros 인터뷰 질문). 엔진 부분(Orca 디스패치·wave·호스트 라우팅)은 버린다.
- 순정 인터뷰: Claude Code 공식 Best practices의 "Let Claude interview you" 프롬프트 (AskUserQuestion → SPEC.md → 새 세션에서 구현).
- 비슷한 도구 조사: [research/nightshift-scan-raw.md](research/nightshift-scan-raw.md). 빌려 볼 후보는 orwa의 체크리스트 변경 감지·작업 잠금·멈춤 감지, marcus의 남은 사용량 예산.

## 구현 순서 의견 (gate 쪽, 2026-10-20)

**루프부터, order는 마지막.** order는 판단이 가장 많고, "어떤 이슈여야 gate가 막히지 않나"는 gate를 돌려 봐야 안다. 그 전까지 이슈는 사람이 템플릿대로 손으로 쓴다(시험 이슈 3~4개면 충분).

1. **시험 저장소 + 손으로 쓴 이슈 4개.** 인수 시험도 손으로 `tests/acceptance/`에 넣는다. 이슈 구성 제안:
   - A: 단순, Done when 명령 하나 → 기본 경로 확인
   - B: A에 blocked-by → 승격 순서 확인
   - C: 일부러 스펙이 모호함 → `spec-question` 경로 확인
   - D: 구현이 인수 시험을 고치고 싶어지게 만든 이슈 → `acceptance-touched` 잠금 확인
2. **init 최소판:** 라벨, 이슈 폼, `.foragent.yml`, launchd 등록. (GitHub App은 v2)
3. **gate 최소 경로:** 승격(라벨 + 해시 + blocked-by + Done when) → runner(스냅샷, worktree, codex, Done when 직접 실행, PR) → `fast` 모드만. 리뷰·재시작 없음.
4. **gate 예외:** 멈춤·너무 큼·재시도·QUESTION.md·이유 코드 댓글. 여기서 A~D가 전부 의도대로 갈라지는지 본다.
5. **inspect 최소판:** 이유 코드로 묶어 읽기 전용 목록 출력 + 한 줄 알림. 객관식 답 기록은 다음.
6. **gate 나머지:** 적대적 리뷰, `batch`/`careful`, `always_human`.
7. **order:** 인터뷰 → 인수 시험 → 이슈 게시. 1~6을 돌리며 모은 "막힌 이유 통계"를 분해 기준에 반영한다.

측정: 각 단계 후 시험 묶음을 다시 돌려 "사람 개입 횟수"와 "다시 손본 PR 비율"을 기록한다. 이 숫자가 공개 판단의 근거가 된다.

## 백로그 (공개 준비 때)
- `fa gate watch`: 레인을 줄로, 이슈를 블록으로(대기 · 실행 중 · 머지 · 질문) 보여 주는 터미널 화면. README 첫 화면 데모 GIF용. (2026-10-20 Jin 아이디어, 필수 아님)
- README 첫 줄 문구: A/B/C 비교 숫자가 나온 뒤.
- 모바일에서 작업 세션 보기: Remote Control(Claude)·`codex remote-control`(Codex). 필요해지면 검토 (2026-10-22 Jin, D55).
