# P2 spec 검토 (gate 쪽, 2026-10-20)

대상: [p2-scheduler.md](p2-scheduler.md). 기준: [gate.md](../gate.md) 3·4·8장, [contract.md](../contract.md) 추가 6, 8-3, 8-4. 코드는 보지 않았다(spec만). 심각한 순서.

## 질문에 대한 답: gate 자신의 라벨 조작이 승인 판정과 섞이나?

**섞이지 않는다. 단, 불변식 하나를 코드와 테스트로 고정해야 한다.**

- gate가 Jin 토큰으로 만드는 승인 관련 이벤트는 `unlabeled approved`뿐이다(5단계 hash-mismatch). 이것은 승인을 **무효로만** 만든다. 같은 계정이라 gate와 Jin을 구분하지 못해도, 방향이 "닫히는 쪽(fail-closed)"이라 안전하다.
- 위험은 gate가 언젠가 `approved`를 **붙이는** 경우뿐이다. 그 순간 gate가 스스로를 승인할 수 있다.
- **요청:** 불변식 "gate·runner 코드는 `approved`를 절대 add하지 않는다"를 명시하고, 테스트로 고정한다(FakeTracker가 `add_label(..., "approved")` 호출을 받으면 실패). P3 이후에도 유지.
- `ready`·`running`·`needs-human`은 승인 판정에 쓰이지 않으므로 gate가 붙였다 뗐다 해도 무관하다.

## 1. [심각] 스냅샷이 "승인 순간"이 아니라 "다음 tick 순간"의 본문을 잡는다

4단계는 tick 시점의 *현재* 본문과 *현재* `origin/main`의 인수 시험으로 스냅샷을 만든다. 그런데 승인 이벤트와 다음 tick 사이에는 최대 5분 틈이 있다.

- 시나리오: Jin이 T0에 approved → T1에 본문이 바뀜(order의 feedback 반영, 에이전트 실수) → T2 tick이 **바뀐 본문**을 스냅샷 → Jin이 보지 않은 본문이 승인된 것으로 고정된다. contract 추가 6이 막으려던 바로 그 구멍이다.
- 인수 시험도 같다: T0~T2 사이에 main의 `tests/acceptance/` 파일이 바뀌면 바뀐 파일이 승인된다.

**수정안:**
- 본문: 이슈의 마지막 본문 편집 시각(GraphQL `Issue.lastEditedAt`, 또는 `userContentEdits`)이 승인 이벤트 시각보다 뒤면 스냅샷을 만들지 않고 `hash-mismatch`로 flag ("승인 뒤 본문이 바뀜"). 편집 기록이 없으면 현재 본문 = 승인 시 본문으로 본다.
- 인수 시험: 승인 시각 기준 base 커밋(`git rev-list -1 --before=<approved_at> origin/<base>`)의 blob과 현재 blob을 둘 다 구해, 다르면 `hash-mismatch`. 같으면 그 값을 스냅샷으로 저장. (비공개 저장소에서 커밋 시각 위조는 v1 위협 모델 밖으로 둔다.)
- 테스트 추가: "승인 이벤트 뒤·첫 tick 전에 본문 편집" → 스냅샷 없음 + hash-mismatch. 인수 시험 파일도 같은 경우.

## 2. [중요] 재승인이 두 동작이 된다 (inspect와 어긋남)

hash-mismatch는 `needs-human`을 붙이고, 2단계는 `needs-human` 이슈를 통째로 건너뛴다. 그러면 재승인하려면 Jin이 **`needs-human` 제거 + `approved` 다시 붙이기** 두 동작을 해야 한다. inspect.md의 "재승인"은 한 동작을 전제로 한다.

**수정안:** `reason=hash-mismatch`로 flag된 이슈에 한해, flag 시각보다 뒤의 유효한 승인 이벤트가 생기면 gate가 `needs-human`을 스스로 떼고 새 스냅샷을 만든다. 다른 reason은 지금처럼 사람이 `needs-human`을 떼야 재개.
- 이를 위해 store에 "마지막 flag reason과 시각"을 남긴다(reasons 댓글을 다시 읽는 것보다 안정적).

## 3. [중요] 이벤트만 보지 말고 현재 라벨도 확인

3단계는 timeline 이벤트만으로 판정한다. timeline API는 페이지가 나뉘고(pagination) 반영이 늦을 수 있다.
- **수정안:** 유효 승인 = "현재 라벨에 `approved`가 있음" **그리고** "이벤트 판정 통과". 둘 중 하나라도 아니면 무효.
- GhTracker의 timeline 조회가 모든 페이지를 읽는지 확인(이벤트가 많은 이슈에서 최신 `unlabeled`를 놓치면 무효인 승인을 유효로 본다).
- approvers 비교는 대소문자 무시(GitHub 로그인은 대소문자를 구분하지 않는다).

## 4. [중요] 죽은 runner가 프로젝트를 영원히 막는다 (P2~P4 사이)

8단계는 `store.active_runs()`가 비어야 시작한다. runner가 비정상 종료해 기록이 남으면 그 프로젝트는 다시 시작하지 못한다. 멈춤 판정은 gate.md 4장(P3/P4 몫)이지만, 그 전에 시험 저장소를 돌리면 바로 걸린다.
- **수정안(P2에 최소한):** tick마다 active run의 pid가 살아 있는지 확인하고, 죽었으면 run을 `outcome=crashed`로 끝내고 `running`→`ready`로 되돌린다. 같은 이슈가 연속 2번 crashed면 `retries-exceeded`로 flag.

## 5. [P3로 넘길 것] 실행 시점의 결속

spec 범위 밖이지만 P3 spec에 들어가야 할 것:
- runner는 지시문과 Done when을 **store의 스냅샷 본문**으로 만든다. 실행 시점에 이슈를 다시 받지 않는다(contract 추가 6-3).
- runner가 worktree를 만든 직후, worktree의 인수 시험 blob을 스냅샷과 한 번 더 대조한다. tick의 재확인(8단계)과 worktree 생성 사이에 main이 움직일 수 있다.
- hash-mismatch가 running 이슈에서 나면 runner를 멈춘다(5단계 주석대로 P3).

## 6. [작은 것]

- **blocked-by 선행이 "not planned"로 닫힌 경우:** 지금은 열려 있지 않으니 승격된다. 선행이 취소됐는데 후행이 도는 건 대개 원하지 않는다. `state_reason == not_planned`이면 `spec-question`류로 flag하자(제안: 새 코드 대신 `spec-question` + "선행 #N이 취소됨").
- **우선순위:** gate.md 4장은 "Projects 우선순위 필드 → 없으면 번호 순". spec은 번호 순만. v1은 번호 순으로 충분하다 — gate.md를 v1 기준으로 맞추겠다(아래).
- **`run_hours: always`:** D15 기본값. `in_run_hours`가 `always`를 받는 테스트가 있는지 확인.
- **API 호출량:** tick마다 승인된 모든 이슈의 본문과 timeline을 읽는다. 이슈 수십 개 × 5분이면 rate limit 안쪽이다. 문제 없음, 기록만.

## gate.md에 반영할 것 (gate 쪽에서 하겠다)
- 4장 우선순위: v1은 번호 순, Projects 필드는 나중.
- 3장: 불변식 "gate는 `approved`를 붙이지 않는다", 스냅샷은 승인 순간 기준(1번), hash-mismatch 재승인은 한 동작(2번).
- 4장: 죽은 runner 처리(4번).
