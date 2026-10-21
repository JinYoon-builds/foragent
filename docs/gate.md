# foragent gate 설계

마지막 갱신: 2026-10-20 (D11–D15 반영). 경계 계약(이슈 형식·라벨·설정·되먹임·승인)은 [contract.md](contract.md)가 정본이고, 이 문서는 gate 내부를 다룬다. 겹치는 내용은 contract.md를 따른다.

## 1. gate는 무엇인가

gate는 **하네스가 아니라 스케줄러**다. 모델과 직접 대화하지 않는다. 언제, 어떤 이슈를, 어떤 실행기로 띄울지만 정한다. 작업 하나 안의 루프는 실행기(Codex, Claude Code)가 돌린다.

| | 하네스 | gate |
|---|---|---|
| 모델과의 관계 | 프롬프트·도구 루프를 직접 돌린다 | 모델과 말하지 않는다 |
| 범위 | 작업 하나 안 | 작업과 작업 사이 |
| 운영체제 비유 | 프로세스 런타임 | 스케줄러 |

설계 기준: "모델이 두 배 똑똑해지면 gate가 덜 필요해지나?" → 아니다. 모델 약점을 메우는 장치(예: "멈추지 마라" 지시)는 gate에 넣지 않는다. 그런 장치는 모델이 좋아지면 상한다.

## 2. 구성

| 부품 | 하는 일 |
|---|---|
| **스케줄러** | 프로젝트마다 하나. launchd로 5분마다 깨어나는 스크립트. **LLM 없음.** 상태 판정·승격·배정만 한다 |
| **runner** | 이슈 하나 실행: 스냅샷 → worktree → 실행기 → Done when 실행 → 리뷰 → PR → 라벨 |
| **세션 지시문** | 실행기에 넣는 짧은 고정 프롬프트 + 승인된 본문 스냅샷. 길어지면 상하므로 짧게 유지 |
| **init 연동** | `foragent init`이 라벨·이슈 폼·`.foragent.yml`·launchd 작업을 깐다 (contract 4번) |

상태의 진실은 **GitHub에만** 둔다. 로컬에는 승인 스냅샷과 실행 기록만 둔다(`~/.local/share/foragent/<repo>/`). 스케줄러가 죽어도 다시 켜면 GitHub 상태에서 이어 간다.

## 3. 상태와 라벨

```
(승인)  ──▶ ready ──▶ running ──▶ 닫힘 (PR의 Closes #N)
                          │
                          └──▶ needs-human
```

- **승인 판정 (D14):**
  - **v1 (개인·비공개 저장소):** `approved` 라벨. 라벨 이벤트의 붙인 사람이 `.foragent.yml`의 `approvers`에 있어야 한다. 붙는 순간 본문과 인수 시험 파일을 스냅샷한다. 알려진 한계: 에이전트가 같은 사람 토큰을 쓰면 구분하지 못한다(D14가 받아들인 위험).
  - **공개 전 (v2):** 사용자 전용 `foragent[bot]` + 사람이 머지한 승인 매니페스트 PR (contract 추가 7). 이때 `approved` 라벨은 표시용이 된다.
- **불변식: gate·runner는 `approved`를 절대 붙이지 않는다.** v1에서 gate도 사람 토큰을 쓰므로, gate가 만드는 승인 관련 이벤트는 `unlabeled approved`(무효화)뿐이어야 한다. 그래야 같은 계정이어도 판정이 닫히는 쪽으로만 섞인다. 테스트로 고정한다.
- **유효 승인 (v1):** 현재 라벨에 `approved`가 있고, 최신 `labeled approved` 이벤트의 붙인 사람이 `approvers`에 있으며(대소문자 무시), 그 뒤에 `unlabeled approved`가 없음.
- **스냅샷은 승인 순간 기준.** 첫 tick이 승인보다 늦게 오므로, 본문의 마지막 편집 시각이 승인 이벤트보다 뒤면 스냅샷을 만들지 않고 `hash-mismatch`. 인수 시험은 승인 시각의 base 커밋 blob과 현재 blob이 같을 때만 스냅샷한다. (검토 기록: [impl/p2-review.md](impl/p2-review.md) 1번)
- **재승인은 한 동작.** `hash-mismatch`로 막힌 이슈는 그 뒤에 유효한 승인이 새로 생기면 gate가 `needs-human`을 스스로 떼고 새 스냅샷을 만든다. 다른 이유의 needs-human은 사람이 떼야 재개한다.
- **ready 승격 조건 (모두 기계 확인):**
  1. 승인됨 (위 방식)
  2. 현재 본문 해시 = 승인 시 해시, 그리고 인수 시험 파일 해시 = 승인 시 해시 (contract 추가 6, 8)
  3. blocked-by 이슈가 모두 닫힘 (같은 저장소, contract 2번)
  4. `## Done when`에 코드 블록이 1개 이상 (contract 1번)
- 조건 4가 빠진 이슈는 승격하지 않고 "기계 검사 없음" 댓글을 단다.

## 4. 스케줄러: 5분마다 하는 판정

| 상태 | 판정 조건 | 행동 |
|---|---|---|
| **쉼** | ready가 있고 running이 없음 | 우선순위 가장 높은 ready → running, runner 실행 |
| **멈춤** | running인데 N분(기본 30) 동안 커밋·세션 출력 변화 없음 | 세션 종료 → 한 번 재시작 |
| **너무 큼** | running이 `max_runtime_minutes`(기본 90)를 넘김 — 진척이 있어도 | 세션 종료, needs-human `reason=timeout` ("쪼개서 다시 order" 추천) |
| **막힘** | 재시작 후 또 멈춤 / 검사 실패가 재시도 상한(기본 2) 도달 / 해시 불일치 / 실행기가 질문을 남김 | needs-human + 이유 코드 댓글(contract 추가 8), 그 이슈는 건너뛰고 다음으로 |
| **끝남** | ready 0, running 0 | 아무것도 안 함. `inspect`가 보여 준다 |

- 판정 근거는 GitHub 상태 + 로컬 실행 기록(pid, 마지막 출력 시각). 프로세스만 보지 않는다.
- 멈춤(진척 없음)과 너무 큼(진척은 있으나 오래 걸림)은 다른 판정이다. order의 "세션 하나로 끝날 크기" 기준을 어긴 이슈는 너무 큼으로 잡힌다.
- 우선순위: v1은 이슈 번호 순. GitHub Projects 우선순위 필드는 나중.
- **죽은 runner:** tick마다 실행 중 기록의 pid가 살아 있는지 본다. 죽었으면 그 실행을 `crashed`로 끝내고 `running`→`ready`. 같은 이슈가 연속 2번 crashed면 `retries-exceeded`.
- 빌려 볼 코드: 멈춤 감지(orwa `hooks/clock-out-gate.sh` 358·366 부근), 작업 잠금·중복 방지(orwa `lib/ownership.sh` 335 부근). spec 작성 시 해당 줄을 직접 읽는다.

## 5. runner: 이슈 하나의 실행 절차

1. **스냅샷 고정.** 승인 시 저장한 본문으로 지시문과 Done when 명령을 만든다. 실행 시점에 본문을 다시 받지 않는다. 이슈 댓글은 넣지 않는다 (contract 추가 6).
2. **worktree.** `~/Dev/<repo>`에서 이슈 브랜치로 worktree를 만든다 (GitHub flow).
3. **실행기.** `executor`·모델(D12)로 세션을 띄운다. 지시문 요지: "Goal을 이루고 Out of scope는 건드리지 않는다. 끝나면 커밋한다. 스펙이 모호해 고를 수 없는 제품 판단이 생기면 `.foragent/QUESTION.md`에 질문과 선택지(추천 표시)를 쓰고 멈춘다." 완료 판정을 실행기의 `/goal`에 맡기지 않는다.
   - 세션이 끝났을 때 `.foragent/QUESTION.md`가 있으면 runner는 검사를 돌리지 않고 그 내용을 이슈 댓글로 옮기고 needs-human `reason=spec-question`. inspect의 "결정" 항목이 된다.
4. **Done when 실행.** 세션이 끝나면 runner가 코드 블록 명령을 **직접** 실행한다. 모두 exit 0이어야 다음 단계. 실패하면 실패 출력을 붙여 같은 세션을 이어서 다시 시킨다(재시도 상한까지).
5. **적대적 리뷰.** 새 세션(가능하면 다른 실행기)이 diff를 리뷰한다. 결함이 나오면 4번처럼 고치게 하고 Done when을 다시 실행한다. 리뷰 반복도 상한을 둔다.
6. **PR.** `Closes #N`, Done when 결과, 체크리스트 항목(기계로 확인 못 한 것), 리뷰 요약을 본문에 넣는다.
7. **머지 판정.** 6장의 검수 강도에 따른다.
8. 작업 잠금 반납, 실행 기록 저장.

## 6. 검수 강도와 머지

기준은 사용자 수가 아니라 **되돌릴 수 있는 정도**다.

| `review` | 머지 | 사람이 보는 것 |
|---|---|---|
| `fast` | CI + Done when 통과 시 main에 자동 머지 | 묶음 끝에 결과물을 써 보기만 |
| `batch` | 묶음 브랜치에 자동 머지, 사람이 묶음째 main에 넣음 | 묶음 단위 검토 |

**`batch`의 묶음 = order의 부모 이슈(Epic) 하나.** 묶음 브랜치는 `foragent/batch-<부모#>`. 자식 PR은 이 브랜치로 머지한다. 주의: GitHub는 기본 브랜치가 아닌 곳에 머지된 PR의 `Closes #N`으로 이슈를 닫지 않는다. 그래서 gate가 자식 이슈를 직접 닫고(“묶음 브랜치에 머지됨” 댓글), 자식이 모두 닫히면 묶음 브랜치 → main PR을 연다. 그 PR은 inspect의 "검토" 항목이 된다.
| `careful` | PR만 열어 둠, 사람이 머지 | PR 하나씩 |

검수 강도와 무관하게 **자동 머지하지 않고 needs-human으로 보내는 경우:**
- diff가 `always_human` 경로를 건드림 (예: `migrations/**`, `auth/**`)
- diff가 **인수 시험 디렉터리(`acceptance_dir`, 기본 `tests/acceptance/`)**를 건드림 (D11, contract 추가 8). Done when 명령에서 경로를 추측하지 않고 이 고정 디렉터리로 판정한다
- 이슈에 `risk:high`
- 배포·외부 공개 (D5)

`review`는 저장소 단위 기본값에 경로별 덮어쓰기를 허용한다. 예: 한 저장소 안에서 `app/`은 fast, `pipeline/`은 careful.

사용자가 많아질수록 진짜 안전장치는 사람의 PR 검토보다 배포 쪽 장치(기능 플래그, 단계적 배포, 빠른 롤백)다. 이건 gate 밖이지만, 이 장치가 있으면 `batch`를 유지할 수 있다.

## 7. 예외와 알림

- 예외 처리는 **규칙**으로 한다(4장 표). LLM은 없어도 돈다.
- **선택 사항: LLM 분류기.** needs-human으로 보내기 전에 실패 로그를 한 번 읽혀 "힌트를 붙여 한 번 더" 또는 "사람 필요"를 고르게 한다. 사람 호출을 줄이고 싶을 때만 켠다.
- **알림:** gate는 사람에게 직접 말하지 않는다. 막힌 이슈에 needs-human과 이유 댓글만 남긴다. 사람에게 말하는 것은 그 프로젝트의 오케스트레이터 하나이고, 묶음 보고는 `fa-inspect`가 한다 (D5, D15). 결정이 필요하고 시간이 급한 경우만 즉시 알림.

## 8. 동시 실행과 시간

- 프로젝트당 동시 실행 1개 (같은 파일 충돌 방지).
- **시간에 묶지 않는다 (D15).** 승인된 이슈가 있으면 언제든 돈다. `run_hours`는 선택 설정이고 기본값은 `always`. 쿼터를 사람 몫으로 남기고 싶을 때만 시간대(예: `22-08`)를 준다.
- 여러 프로젝트를 묶는 **공용 슬롯은 v1에 넣지 않는다.** 쿼터는 총량을 정할 뿐이라 슬롯이 처리량을 늘리지 않는다. 맥미니 자원 부족(Xcode 빌드 겹침)이나 쿼터 문제가 실제로 보이면 붙인다.
- 쿼터 한도에 걸리면 기다렸다 이어 간다(멈춤으로 판정하지 않는다). 남은 사용량 기반 예산은 marcus `internal/budget/budget.go` 199 부근을 참고 후보로 둔다.

## 9. 설정: `.foragent.yml`

```yaml
review: batch            # fast | batch | careful
review_paths:            # 경로별 덮어쓰기 (선택)
  app/**: fast
  pipeline/**: careful
always_human:
  - migrations/**
  - auth/**
executor: codex          # codex | claude
executor_model: null     # null이면 전역 기본값 (D12, foragent config)
codex_goal: false        # 실험 기능: Codex 프롬프트에 /goal 조건을 붙임
reviewer: claude         # codex | claude | none (기본 = executor와 다른 것)
reviewer_model: null
review_level: medium    # Claude /code-review: low | medium | high | xhigh | max
review_max: 2            # 리뷰 FAIL 허용 수; 초과하면 PR을 열고 risk-high로 보류
approvers: [JinYoon-builds]   # v1 승인 라벨을 붙일 수 있는 사람 (D14)
acceptance_dir: tests/acceptance/   # order가 구현 전에 쓰고 잠그는 인수 시험 (D11)
notify: slack:#proj-dev  # 프로젝트 오케스트레이터 창구 (형태·개수 자유)
run_hours: always         # 선택: "22-08"처럼 시간대 제한
stall_minutes: 30
max_runtime_minutes: 90
max_retries: 2
```

init이 기본값을 쓰고, 사람이나 init 옵션으로 고친다. 기존 파일에서는 명시한 옵션의 키가 없을 때만 추가하고, 이미 있는 값은 보존한다. 모델은 전역 기본값(`foragent config`) + 이 파일의 덮어쓰기 (D12).

Claude 구현 세션은 `/goal`과 실행별 deny 규칙·Stop 훅을 쓴다. Stop 훅은 승인 스냅샷의 Done when 명령을 저장소 루트에서 실행하며, 실패하면 최대 8회 종료를 막는다. 이 한도는 runner의 재시도 사이에도 유지된다. 세션이 끝나면 runner가 독립적으로 다시 검증한다.

Claude 리뷰는 스테이징한 구현에 `/code-review <review_level>`을 실행한다. 이 스킬은 결함을 검토하며 Out of scope·목표 적합성은 별도로 판정하지 않는다. Done when과 인수 시험 잠금이 목표를 보장한다. Codex 리뷰는 기존 목표·범위·diff 프롬프트를 사용한다.

## 10. 갈아끼울 축 (v1 구현은 굵게)

| 축 | v1 | 나중 후보 |
|---|---|---|
| 일감 저장소 | **GitHub Issues + Projects** | Linear(상태가 표준이라 쉬움), Jira(상태·전이 매핑 설정 필요) |
| 코드·검증 | **GitHub + Actions** | GitLab |
| 실행기 | **Codex, Claude Code** | — |
| 알림 | **오케스트레이터 창구로 넘김** | — |

일감 저장소와 코드 호스트는 다른 축이다(Linear를 써도 코드는 GitHub에 있다). 핵심 상태 기계는 GitHub 명령을 직접 부르지 않고 이 인터페이스만 거친다. 두 번째 어댑터는 실제로 필요한 사용자가 생길 때 만든다.

## 11. 비슷한 도구와의 차이

- **OpenAI Symphony** (스타 약 2.8만, 2026-02 공개, 0.0.3, 월 1~2커밋): 같은 발상(이슈 보드 = 컨트롤 플레인). gate의 차이는 **입구와 출구의 관문**: 승인된 본문만 실행, Done when 직접 실행, 검수 강도·항상 사람 경로. 가운데 스케줄러는 빌리거나 참고할 대상. SPEC과 코드는 아직 직접 읽지 않았다.
- **orwa-mahmoud/nightshift, marcus/nightshift**: 이슈 큐를 외부 검증하며 당겨 오지 않는다 ([research/nightshift-scan-raw.md](research/nightshift-scan-raw.md)).
- **공식 `/goal`** (Claude Code, Codex 실험): 작업 하나를 끝까지 돌리는 것은 흡수됐다. 큐에서 새 컨텍스트로 하나씩 꺼내는 것은 흡수되지 않았다. gate는 후자만 한다.

## 12. 남은 것

- Symphony SPEC 직접 읽기 → 가운데 스케줄러를 빌릴지 판단
- 빌려 볼 코드 줄 확인 (orwa 344·358·366, ownership.sh 335, marcus budget.go 199)
- 멈춤 판정에 쓸 "세션 출력 변화"의 구체 신호 (실행기별)
- 시험: 이슈 3~5개짜리 시험 저장소로 쉼·멈춤·막힘 판정 확인

## 운영 메모: gate 고정본 올리기 (2026-10-21)
- `uv tool install --force .`만으로는 같은 버전(0.1.0)의 캐시된 빌드가 재사용되어 **예전 코드가 그대로 남는다**(실제로 리뷰 해석기 수정이 반영되지 않아 #2·#7이 다시 멈춤).
- 반드시 `uv tool install --force --reinstall .` 후 `diff -rq src/foragent <설치 경로>/foragent --exclude=__pycache__`로 같음을 확인한다. 나중 후보: `fa --version`에 커밋 SHA를 넣어 한 줄로 확인.
