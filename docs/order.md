# foragent order — 설계 초안

상태: 초안 (2026-10-20). 결정 대기 항목은 맨 아래.

## 하는 일

사람과 인터뷰해서, gate가 밤새 처리할 수 있는 이슈 묶음을 만들고, 한 번에 승인받는다.

```
입력 모으기 → 인터뷰 → 되짚기(restate) → 쪼개기 → 점검표 → 일괄 승인 → 이슈 게시
```

결과의 품질은 여기서 정해진다. gate는 이슈에 적힌 대로만 일한다.

## 1. 입력 모으기 (질문하기 전에)

order는 질문하기 전에 스스로 찾을 수 있는 것을 먼저 찾는다.

- **되먹임:** `needs-human` 이슈와 그 댓글, `feedback` 라벨 이슈. 매일 order의 첫 재료다.
- **이미 정해진 것:** 저장소의 SPEC 문서, `docs/decisions.md`, 열린 이슈, 코드.
- **기다리는 일:** 열린 이슈마다 본문의 `## Waiting` 구역(이유, 다음 조건, 맡은 역할)이 `fa order context`의 `open_issues[].waiting`에 그대로 나온다. 승인 전 이슈도 포함한다.
- **사람의 새 요청:** order를 부를 때 한 말.

`fa order context --json`의 `other_orders`는 다른 주문자의 열린 이슈 번호·제목·주문자·
`Files`를 보여 줘요. 번호로 기존 이슈에 링크하고 `open_issues`의 현재 상태와 함께
새 주문의 파일 범위가 겹치는 사실을 보여 줘요. 사람은 기다리기 / 파일 나누기 /
그대로 진행 중에서 골라요. 경로와 glob은 적힌 그대로 보존해요. `Files`가 비었거나
없으면 범위를 모르는 것이므로 겹침이 없다고 판단하지 않아요.

이슈 질문은 유효 주문자(effective orderer)의 공장에 있는 기획 세션에서만 답해요.
`decisions`에 나온 기존 @orderer GitHub 이슈 댓글과 결정 흐름을 그대로 사용해요.
다른 사람의 질문은 `other_orders`에서 읽을 수 있는 사실로 남아요. 소유자 판정 거절은
설정 언어로 `open_issues[].owner_error`에 나와요. 로그인 조회가 실패하면 `identity`에
이유를 보여 주면서 레거시·기존 단일 공장의 질문은 `decisions`에 유지해요. 이때 주인
라벨이 있는 이슈는 `other_orders`에서 계속 보여 주되 공장 계정은 추측하지 않아요.
모순된 라벨이나 주인 미확정 표식이 있는 이슈를 레거시로 다루지는 않아요. 소유자 라벨이
없고 `legacy_owner` 설정도 없는 기존 단일 공장에서는 기존 질문 흐름을 유지해요.

## 2. 인터뷰

순정 방식을 쓴다: `AskUserQuestion` 도구로 객관식 질문, 추천안 표시. Ouroboros MCP에는 의존하지 않는다. 대신 Ouroboros에서 쓸모 있는 구조 셋만 가져온다.

- **모호함 장부:** 아직 안 정해진 것 목록을 인터뷰 내내 보이게 유지한다. 장부가 비면 인터뷰를 끝낸다.
- **되짚기 관문:** 이슈를 만들기 전에 "이렇게 이해했다"를 한 문단으로 보여 주고 확인받는다.
- **닫힘 점검:** 아래 4장의 점검표.

**무엇을 묻나 (옛 delion-wave 질문 거르기 규칙 유지)**

| 종류 | 예 | 처리 |
|---|---|---|
| 저장소 사실 | 지금 스키마, 테스트 명령 | 묻지 않고 직접 찾는다 |
| 되돌릴 수 있는 구현 세부 | 함수 이름, 파일 배치 | 묻지 않고 구현자가 정한다 |
| 사람의 제품·취향 판단 | 화면 흐름, 문구, 우선순위, 범위 | **묻는다.** 선택지마다 결과와 추천을 붙인다 |
| 이미 합의된 것 | 앞선 결정, SPEC | 묻지 않고 전제로 쓴다 |

**두 가지 길이**
- 새 프로젝트(init 직후): 제품 목표부터 찾는 깊은 인터뷰.
- 매일: 되먹임 처리 + 새 요청만. 정해진 것은 다시 묻지 않으므로 짧다.

## 3. 쪼개기

**한 이슈 = PR 하나로 끝나고, 따로 확인할 수 있는 단위.** 옛 delion-plan의 분해 기준을 그대로 쓴다. 다음 중 하나라도 다르면 나눈다.

- 결과물(따로 머지할 수 있는가)
- 확인 환경(로컬·CI / 실기기 / 스테이징 / 운영)
- 승인 경계(사람이나 외부의 승인이 필요한가)
- 한쪽이 막혀도 다른 쪽은 끝낼 수 있는가

추가 기준(gate용):
- **Codex 세션 하나로 끝날 크기.** 너무 크면 gate가 멈춤으로 판정한다.
- **의존성은 GitHub blocked-by로만** 표시한다. 본문에 "#3 다음에"라고 쓰지 않는다.
- 묶음 전체의 목표는 부모 이슈(Epic)에 적는다. 부모는 실행 대상이 아니다.

## 4. 점검표 (게시 전)

이슈마다 아래에 답할 수 있어야 한다. 하나라도 답이 없으면 더 쪼개거나 다시 묻는다.

1. `Close when <구체적 증거>`를 한 문장으로 쓸 수 있는가?
2. 그 증거를 **기계가 실행할 수 있는가?** (→ `## Done when` 코드 블록)
3. 기계로 못 하는 확인은 무엇인가? (→ 체크리스트, 사람 검토로 감)
4. 코드와 CI만으로 닫을 수 있는가, 아니면 실기기·스테이징·사람 판단이 필요한가?
5. 빼 둔 것은 무엇인가? (→ `## Out of scope`)

사람에게 보여 줄 때는 한 표로 보여 준다.

| # | 이슈 | 확인 명령 | 사람 확인 | 막는 이슈 |
|---|---|---|---|---|

## 5. 일괄 승인

이슈 목록과 점검표를 보여 주고 **한 번에** 승인받는다: [전부 승인] [일부 빼고] [수정할 게 있음].

승인을 GitHub에 남기는 방식은 승인 신원 결정(decisions.md Q1)을 따른다. 유력안: order가 "승인 요청 PR"을 열고 사람이 폰에서 머지한다.

## 6. 게시

- 이슈 형식은 gate가 깐 템플릿을 따른다([contract.md](contract.md) 1번).
- 인터뷰 결과는 저장소에 남긴다: `docs/specs/YYYY-MM-DD-<주제>.md`. 다음 order가 "이미 정해진 것"으로 읽는다.
- 되먹임 이슈는 처리 결과에 따라 닫거나, 새 이슈로 바꾸거나, Done when을 고친다. Done when을 고치면 재승인이 필요하다.
- 기존 이슈를 고치는 `edits[]`는 `## Waiting` 구역과 `blocked_by`(이번 계획의 key)를 함께 바꾼다. `blocked_by: []`는 의존을 지우고, 빼면 기존 의존을 둔다. 의존만 바꿔도 재승인이 필요하다.
- `## Waiting` 구역이 바뀌면 그 이슈에 설정 언어로 댓글 한 줄을 남긴다. 사람이 손으로 쓴 대기를 지워도 마찬가지다. 그대로면 댓글을 남기지 않는다.
- `edits[]`는 기존 Done when 명령과 체크리스트 글을 열린 이슈든 닫힌 이슈든 그대로 둔다. 줄을 지우려면 `done_when_removals`에 `{"line": "정확한 글", "reason": "이유"}`로 한 줄씩 적는다. 빠졌거나, 이유가 비었거나, 지금 Done when에 없는 줄을 적으면 발행·`--dry-run`·preread가 쓰기와 모델 호출 전에 이슈 번호와 그 줄을 보여 주고 거절한다. 이유는 판단하지 않는다. 지운 줄, 지운 주문, 이유는 이슈 본문의 `## Removed from Done when`에 남고 이후 수정에도 이어진다. 이 기록은 그 줄을 지운 주문의 재발행(발행, preread, `--approve`)에만 통하고, 다른 주문이 같은 줄과 이유를 적으면 거절한다.
- `fa order edit NUMBER --json`은 이슈의 지금 구역을 `edits[]` 항목 하나로 출력한다(읽기만 한다). 이것을 고쳐서 계획에 넣는다.

## 결정 대기

### O1. 확인 테스트를 누가 쓰나 — 결정됨 (decisions.md D11)

Done when이 `pnpm test -- login` 같은 테스트를 가리킬 때, 그 테스트를 **구현자(Codex)가 직접 쓰면** 통과하기 쉬운 약한 테스트를 쓸 수 있다. 시험 문제를 응시자가 내는 셈이다.

| 안 | 내용 | 장점 | 단점 |
|---|---|---|---|
| A. order가 먼저 쓴다 | order가 인수 테스트(acceptance test)를 미리 써서 승인 요청 PR에 함께 넣는다. 구현자는 그 파일을 못 고친다(고치면 사람에게 감) | 시험 문제와 응시자가 분리된다. 승인 PR 하나에 이슈 승인과 테스트 승인이 같이 들어간다 | order가 인터페이스(함수 이름, API 형태)를 미리 정해야 한다. order 시간이 길어진다 |
| B. 바깥에서만 확인 | Done when에는 실행 결과만 확인하는 명령을 쓴다(스모크 스크립트, curl, CLI 출력 비교) | 구현 방식에 묶이지 않는다 | 세밀한 동작은 확인하기 어렵다 |
| C. 구현자가 쓰고 리뷰어가 본다 | 구현자가 테스트도 쓰고, 적대적 리뷰어가 테스트가 충분히 엄격한지 본다 | order가 가볍다 | 리뷰어의 판단에 기댄다. "모델의 말을 믿지 않는다"는 원칙이 약해진다 |

**결정 (2026-10-20 Jin): A를 기본으로, A가 어려운 이슈(UI 등)는 B.** 작은 단위 테스트는 구현자가 자유롭게 쓴다. 최종 시험(Done when)만 order 단계에서 구현 전에 정하고 승인과 함께 잠근다. 보장은 모델이 아니라 구조(시점 + gate의 경로 잠금)에서 나오므로 어떤 모델 조합에서도 성립한다.

## Optimistic file overlap (#448)

`overlap_per_file` is a positive integer, default **2**: a cautious start from one
observed conflict (#196) in 231 runs. The orchestrator changes it only using
conflict evidence from `fa orch facts`; the facts display is a separate issue.

For one file, the orchestrator can set its own limit from that file's records:
`fa orch plan --overlap PATH=N --why "<evidence>" --by orchestrator [--expires ISO]`
(`PATH=off` clears it). N counts every run sharing the file, the new one included.
A live entry replaces `overlap_per_file` for that exact file at admission and at
the runner's late-file wait; a glob claim meets every entry it may cover and the
fallback. Exclusive reservations, union files and budgets still apply. An entry
keeps its author, reason and expiry; an orchestrator never changes a person's
entry. `overlap_mode: fixed` in `.foragent.yml` ignores the entries and applies
`overlap_per_file` to every file (default `measured`).

Plan `issues[]` and `edits[]` accept `exclusive: [path, glob]`, rendered as YAML
metadata immediately after the version marker. These reservations remain locked
even when effective Files narrow. Other Files may share up to the configured
number of runs across all lanes, including main and lane-less issues. Empty
Files and missing/currently mismatched approvals retain their own-lane rule.

Until the first own commit the scheduler reserves declared Files. Afterwards it
uses concrete changed paths against the merge base with the run's target branch,
including later working-tree changes and deletions but excluding inherited target
changes and injected runner artifacts. A late undeclared path waits inside the
runner before another implementation or merge if it crosses an exclusive
reservation or the cap. Paths released by effective-file narrowing are late
additions if re-added, even when originally declared. Runs without late additions
keep their place; expanding runs yield all claims, including exclusive reservations,
to earlier expanding runs (issue number breaks equal start times). Waiting
uses the existing credited wait and `waiting_until`, outside the merge lock.
Measurements publish together only after every Git command succeeds; unavailable
worktrees retain the previous measurement, and legacy unknown effective Files fall
back to declarations.

Each execution records concrete overlapping files and peer issue numbers, whether
the merge conflicted, resolution seconds, and rebuild count. Peers that finish
before merge remain in the record. Resolution time excludes credited waits and
the subsequent implementation rebuild. New helper modules own this bookkeeping;
no new interpretation of issue meaning or conflict-resolution policy is added.

- Accepted limit (#448): scheduler admission can race the final late check (TOCTOU); a soft-lock breach costs one merge conflict, which the runner resolves.
- Accepted limit (#448): multi-way glob intersections do not guarantee Unicode literal-alias equivalence such as Straße/STRASSE.

Publication checks the current GitHub title and body against the fields this plan
last read, for both existing children and explicit edits. If either changed,
`fa order publish` stops before changing files, issues, labels or approvals. The
diagnostic identifies each issue and shows the old and current values, including
changed Done when commands. This applies with or without `--approve`; `--dry-run`
remains a preview.

Run `fa order preread PLAN.json` to explicitly re-read: the implementer sees the
current GitHub title and full body before the observed base is refreshed. A statement
issue has no reader turn, so the command itself prints those fields for the agent that
ran it (`current` with `--json`) and refreshes the base. Resolve
the requirements in the plan before publishing again; no automatic merge chooses
which requirement wins. Each successful issue write immediately records the title
and body it wrote as the next base, before labels, links or comments are updated.
If a later publication step fails, retrying can continue from those writes. The local `.foragent/order-observed.json` stores these
observations by plan id and issue number. Legacy targets without a recorded base
publish as before. Labels, comments and native dependency edges are not compared.
