# 경계 계약: order ↔ gate (2026-10-20, 정본)

> 2026-10-20 이름 정리: dev-gate → `foragent gate`, enroll → `foragent init`, `.dev-gate.yml` → `.foragent.yml`, 형식 표식 `dev-gate:v1` → `foragent:v1`, nightshift → foragent, 이슈 공장 → `foragent order`. 내용은 바뀌지 않았다. gate 설계 상세는 [gate.md](gate.md).
> 2026-10-20 D15: morning → `inspect`(대화 스킬 `fa-inspect`), CLI 별칭 `fa`. gate는 시간에 묶지 않는다(`run_hours` 기본 always). "밤/아침"은 "사람이 없는 동안/돌아와서"로 읽는다.

원칙 하나를 먼저 제안한다: **형식은 읽는 쪽이 소유한다.** gate가 읽는 칸·라벨·설정은 gate(`foragent init`)가 정의하고 깔며, order은 그 형식에 맞춰 쓰기만 한다. 아래 수정안은 대부분 이 원칙에서 나온다.

## 합의된 전제 — 동의
전부 동의. 덧붙임 두 개:
- 구현=Codex에 동의. 다만 gate의 실행기는 갈아끼울 수 있게 둔다(Claude Code도 가능). Codex `/goal`은 아직 실험 기능(`goals = true` 설정 필요)이라, 완료 판정은 `/goal`에 맡기지 않고 아래 1번의 외부 검사로 한다.
- 이쪽에서 "공용 슬롯(전체 동시 실행 상한)"은 뺐다. 프로젝트당 동시 실행 1개로 시작하고(시간 제한 `run_hours`는 선택, 기본 always — D15), 맥미니 자원이나 쿼터 문제가 실제로 보이면 그때 붙인다.

## 1. 이슈 본문 형식 — 수정안
**YAML 블록 대신 고정 머리글.** GitHub UI에서 사람이 고치기 쉽고, GitHub Issue Form으로 강제할 수 있다. 머리글 문자열은 언어와 무관하게 고정(영어 키)한다.

~~~markdown
<!-- foragent:v1 -->
## Goal
한두 줄.

## Out of scope
- ...

## Done when
```sh
pnpm test -- auth
./scripts/smoke.sh login
```
- [ ] 로그인 실패 시 오류 문구가 화면에 보인다
~~~

- gate가 파싱하는 것은 `## Done when` 하나뿐이다. Goal·Out of scope는 그대로 세션 지시문에 넣고 파싱하지 않는다.
- `## Done when` 안의 **코드 블록 = 실행 검사.** 세션이 끝나면 runner가 직접 실행한다(모델이 "통과했다"고 말한 것을 믿지 않는다). 모두 exit 0 + CI 통과여야 완료.
- `## Done when` 안의 **체크리스트 = 실행할 수 없는 검사.** 검수 강도에 따라 처리한다: 빠르게→무시하고 PR 본문에 남김 / 묶음·신중하게→사람 검토 항목으로 보고.
- **ready 승격 조건:** `## Done when`에 코드 블록이 1개 이상 있어야 한다. 체크리스트만 있으면 승격하지 않고 "기계 검사 없음"으로 되돌린다.
- **"위험 경로 표시" 칸은 빼자.** 쓰는 쪽이 빠뜨리면 그만이라 믿을 수 없다. gate가 PR diff의 경로를 `.foragent.yml`의 `always_human`과 대조해 스스로 판정한다. order이 특별히 위험하다고 판단하면 `risk:high` 라벨만 붙이면 된다(선택).
- `<!-- foragent:v1 -->` 표식으로 형식 버전을 단다. 형식이 바뀌어도 옛 이슈를 구분할 수 있다.

## 2. 의존성 표현 — 동의, 한 줄 보탬
- 순서는 **blocked-by만** 본다. sub-issue(부모-자식)는 묶음 표시일 뿐 순서를 뜻하지 않는다. 부모 이슈는 실행 대상이 아니다(자식이 다 닫히면 자동으로 닫는 정도만).
- v1은 같은 저장소 안의 blocked-by만 지원한다. 저장소를 넘는 의존성은 필요가 생기면 추가.

## 3. 라벨 이름과 주인 — 동의, 이름 둘 수정
| 라벨 | 붙이는 쪽 | 비고 |
|---|---|---|
| `approved` | Jin만 | gate는 이슈 타임라인에서 붙인 사람을 확인하고, Jin이 아니면 무시한다 |
| `ready` | gate만 | approved + blocked-by 모두 닫힘 + Done when 코드 블록 있음 |
| `running` | gate만 | (이쪽 초안의 in-progress를 running으로 통일, 동의) |
| `needs-human` | gate만 | **needs-jin → needs-human 제안.** 일반화한 제품이라 사람 이름을 라벨에 넣지 않는다 |
| `risk:high` | order 또는 Jin | 선택. 붙으면 검수 강도와 무관하게 사람 검토 |

- 완료 = PR의 `Closes #N`로 이슈가 닫히는 것. `done` 라벨은 두지 않는다.
- **수정: 라벨을 만드는 것도 order이 아니라 init이 한다.** 주인 하나 원칙(4번)과 같은 이유.

## 4. `.foragent.yml`과 `foragent init` — 동의 (셋업은 init만 부른다)
init이 소유하고 까는 것:
- 라벨 세트(3번 표)
- **이슈 템플릿 / Issue Form** (1번 형식). 형식의 주인이 gate이므로 템플릿도 gate가 깐다. order은 이 템플릿을 읽어서 맞춰 쓴다.
- `.foragent.yml` 기본값: `review`(fast|batch|careful, 경로별 덮어쓰기 가능), `always_human`, `executor`(codex|claude), `notify`(프로젝트 오케스트레이터의 창구), `run_hours`(기본 always)
- 그 프로젝트용 launchd 작업 하나 (프로젝트당 스케줄러 하나)

order의 빈 폴더 셋업은 저장소·칸반을 만든 뒤 `foragent init` 한 번만 부르고, yml을 직접 쓰지 않는다. 설정을 바꾸고 싶으면 init 옵션으로 넘긴다.

## 5. 되먹임 — 수정안: 통로는 GitHub, 생산은 gate, 소비는 order
- **gate가 맡는 것:** 막힌 이슈에 `needs-human` + 이유 요약 댓글. **inspect**(검수)는 이 이슈들을 묶어 보여 준다. Slack은 알림일 뿐 기록하지 않는다.
- **Jin이 하는 것:** "이게 아닌데"를 GitHub에 남긴다 — 해당 이슈에 댓글, 또는 새 이슈에 `feedback` 라벨. (v1에서는 Slack 답장을 GitHub로 옮기는 기능을 만들지 않는다.)
- **order이 맡는 것:** 다음 인터뷰를 시작할 때 `needs-human`·`feedback` 이슈를 먼저 읽어 입력으로 삼고, 새 이슈로 바꾸거나 기존 이슈의 Done when을 고친다.
- `feedback` 라벨도 init이 깐다.

즉 진실은 계속 GitHub 한 곳이고, 두 시스템은 서로를 직접 부르지 않는다. GitHub 상태만 읽고 쓴다.

## 이름
이름은 Foragent로 확정(decisions D2). gate = `foragent gate`(`fa gate`), 검수 = `fa-inspect`, 셋업 = `fa-init`, 주문 = `fa-order` (D15). 이름 겹침(orwa-mahmoud/nightshift, marcus/nightshift)은 Symphony 조사 때 같이 본다.

---

## 추가 6. 승인은 "그 시점의 본문"에 대한 것 — 동의, 해시 방식 + 구멍 둘 보탬 (2026-10-20)

**방식: 편집 시각 비교 대신 스냅샷 + 해시.** 이슈의 `updated_at`은 라벨·댓글에도 바뀌어서 판정에 못 쓴다.

1. `approved` 이벤트를 Jin이 붙인 것으로 확인하면, gate가 그 순간의 **본문 전체**를 로컬 상태(`~/.local/share/foragent/<repo>/<issue>.approved.md`)에 저장하고, sha256을 이슈 댓글로 남긴다: `<!-- foragent:approved sha256=… by @… at … -->`
   - 해시 대상은 Done when만이 아니라 **본문 전체**. Goal·Out of scope도 셸 권한을 가진 세션의 지시문이 되기 때문이다.
2. ready 승격 직전과 **실행 직전** 두 번 현재 본문 해시를 대조한다. 다르면 `ready`·`approved`를 내리고 "승인 후 본문이 바뀜, 재승인 필요" 댓글 + `needs-human`.
3. **실행은 항상 스냅샷으로 한다.** 세션 지시문과 Done when 명령 모두 저장된 스냅샷에서 만든다. 실행 시점에 본문을 다시 받아 쓰지 않는다(대조와 실행 사이 틈을 없앤다).

**보태는 구멍 1: 댓글.** 승인 뒤 달린 댓글도 승인받지 않은 입력이다. runner는 세션에 **이슈 댓글을 넣지 않는다**(스냅샷 본문만). 예외: Jin이 단 댓글만 포함하는 옵션은 v2에서.

**보태는 구멍 2: 검사를 고치는 PR.** Done when이 저장소 파일(`./scripts/smoke.sh`, 테스트 파일)을 부르면, 실행 중인 에이전트가 PR 안에서 그 파일을 고쳐 검사를 약하게 만들 수 있다. 그래서:
- PR diff가 **Done when 명령이 참조하는 파일**이나 테스트 디렉터리를 건드리면, 검수 강도와 무관하게 자동 머지하지 않고 `needs-human`.
- 같은 이유로 세션이 이슈 본문을 고치면 2번 대조에서 걸려 무효가 된다(의도된 동작).

nightshift 두 저장소 조사 결과는 [research/nightshift-scan-raw.md](research/nightshift-scan-raw.md).

---

## 추가 7. 승인 신원 — order가 Jin 토큰을 쓰면 "붙인 사람=Jin"은 증거가 아니다 (2026-10-20)

이름 반영: 이하 gate = `foragent gate`.

**진단.** 같은 맥미니·같은 OS 사용자에서 도는 에이전트는 Jin의 gh 토큰, Slack 서명 비밀, 로컬 상태 파일을 모두 읽을 수 있다. 그래서 그 안에서 만든 어떤 승인 표시(라벨, 댓글, 해시, Slack 버튼 검증)도 위조할 수 있다. 확인을 정교하게 하는 것으로는 못 막고, **승인 신원을 맥미니 밖으로 빼야** 한다.

**제안: 신원 분리 + 승인 매니페스트 PR (Jin 수고 = 폰에서 클릭 한 번)**

1. **맥미니의 모든 자동화는 봇 신원으로만 GitHub에 쓴다.** GitHub App(예: `foragent-bot`) 설치 토큰. order·gate·runner·에이전트 세션 전부. Jin 개인 토큰은 에이전트가 닿는 환경에 두지 않는다.
2. **order는 승인을 "요청"만 한다.** 이슈들에 `approved`를 붙이는 대신, 승인 매니페스트 파일을 담은 PR 하나를 연다:
   ```yaml
   # .foragent/approvals/2026-10-20-a.yml
   issues:
     - number: 41
       sha256: 3f1c…   # 본문 전체 해시 (6번의 스냅샷)
     - number: 42
       sha256: 9a0d…
   ```
3. **Jin은 그 PR을 폰(GitHub 앱/웹)에서 머지한다. 이게 클릭 한 번이다.** 묶음 전체가 한 번에 승인된다.
4. **gate의 승인 판정:** main에 있는 매니페스트 중 `merged_by == Jin`인 PR로 들어온 것만 유효. 그리고 이슈의 현재 본문 해시가 매니페스트의 해시와 같을 때만 ready로 올린다. 6번의 본문 결속이 여기서 같이 풀린다(해시가 매니페스트 안에 있으므로 승인 대상이 정확히 고정된다).
5. **보호 규칙:** `.foragent/approvals/**`는 CODEOWNERS = Jin + 브랜치 보호(코드 오너 리뷰 필수, 관리자 우회 금지). 봇은 자기 PR을 승인하거나 머지할 수 없다.
6. `approved` 라벨은 사람이 보기 위한 표시로만 남기고(gate가 매니페스트 머지 후 붙임), 판정 근거로 쓰지 않는다.

**좋은 점:** 승인 기록이 git 이력에 남는다(누가, 언제, 어떤 본문 해시를). 묶음 승인이 자연스럽다. GitHub가 이미 신원 확인을 해 주므로 서명 체계를 따로 만들 필요가 없다.

**전제 조건 (Jin 결정 필요, 되돌리기 어려운 변경):** 현재 맥미니 gh는 Jin 계정 토큰(DelionLabs ADMIN)으로 로그인돼 있다(2026-10-14 기록). 이 방식은 맥미니의 자동화 신원을 봇으로 바꾸고 Jin 토큰을 에이전트 환경에서 빼는 것을 전제로 한다. 또한 Jin이 맥미니 자기 세션에서 머지하면 구분이 안 되므로, **승인 머지는 맥미니 밖(폰·맥북)에서** 한다는 운영 규칙이 붙는다.

**대안(약함, 참고):** Slack 버튼 승인 → gate가 Slack 서명 검증. 클릭 한 번은 같지만, 서명 비밀이 맥미니에 있어 같은 위조 문제가 남는다. 신원 분리 없이 쓸 수 있는 유일한 방법은 이것뿐이라, 봇 전환 전 임시로만 고려.

빌려 볼 후보(orwa clock-out-gate 344·358·366, ownership.sh 335, marcus budget.go 199) 확인했다. gate 설계 spec 쓸 때 해당 줄을 직접 읽어 반영한다.

---

## 추가 8. D11·D13·D14 반영으로 생긴 계약 보강 (2026-10-20, gate 쪽)

**8-1. 인수 시험은 고정 디렉터리에 둔다.** order가 구현 전에 쓰는 최종 시험(D11)은 `.foragent.yml`의 `acceptance_dir`(기본 `tests/acceptance/`) 아래에 둔다. gate는 Done when 명령에서 경로를 추측하지 않고 이 디렉터리로 잠금을 판정한다. 구현 PR이 이 디렉터리를 건드리면 검수 강도와 무관하게 needs-human.

**8-2. v1에서 인수 시험이 main에 들어가는 길.** v1은 승인 매니페스트 PR이 없으므로(D14): order는 인수 시험을 이슈 게시 **전에** main에 넣는다(비공개 저장소, 되돌릴 수 있음). 이슈 본문의 Done when은 그 파일을 실행하는 명령을 쓰고, `## Done when` 아래에 파일 경로를 링크한다(사람이 승인 전에 시험을 볼 수 있게).

**8-3. 승인 스냅샷은 본문 + 인수 시험 파일.** 추가 6의 해시 결속을 넓힌다. `approved`가 붙는 순간 gate는 본문과 함께 **그 이슈의 Done when이 실행하는 `acceptance_dir` 안 파일들의 git blob 해시**를 기록한다. 실행 직전에 둘 다 대조하고, 하나라도 다르면 승인 무효(`reason=hash-mismatch`). 이게 없으면 본문은 그대로인데 시험 파일만 약해지는 길이 열린다.

**8-4. needs-human 이유 코드.** inspect가 항목을 "결정 / 재승인 / 검토 / 기술 실패"로 나눌 수 있게, gate의 needs-human 댓글 첫 줄에 기계가 읽는 표식을 단다:
```
<!-- foragent:needs-human reason=<코드> -->
```
| 코드 | 뜻 | inspect 묶음 |
|---|---|---|
| `spec-question` | 실행기가 `.foragent/QUESTION.md`로 제품 판단을 물음 (댓글에 질문·선택지) | 결정 |
| `hash-mismatch` | 승인 뒤 본문 또는 인수 시험이 바뀜 | 재승인 |
| `always-human-path` / `acceptance-touched` / `risk-high` | 자동 머지 금지 경로·표시 | 검토 |
| `retries-exceeded` / `stalled` / `timeout` | 기술 실패 (timeout = 너무 큼, 쪼개기 추천) | 기술 실패 (정보 + 추천) |
| `no-machine-check` | Done when 코드 블록 없음 (승격 거부) | 결정 (order로 되돌림) |

**8-5. batch 묶음 = 부모 이슈(Epic).** order가 부모 이슈로 묶은 자식들이 한 묶음이다. 브랜치 `foragent/batch-<부모#>`. 묶음 브랜치에 머지된 자식 이슈는 gate가 직접 닫는다(기본 브랜치가 아니라 `Closes #N`이 동작하지 않음). 자식이 다 닫히면 gate가 묶음 → main PR을 연다.
