# Foragent

> Your agent forages for the next task.

한 작업을 끝까지 하는 것은 이제 순정 Claude Code·Codex도 잘한다. Foragent는 **작업과 작업 사이**를 자동화한다.

```
작업 하나가 끝남 → 완료 확인 명령 실행 → 적대적 리뷰 → 통과 → 다음 이슈를 스스로 가져옴
```

사람은 무엇을 만들지 결정하고 승인만 한다. "다 했어? → 리뷰해 → 다음 거"를 손으로 반복하지 않는다.

## 명령

| 단계 | 어디서 | 하는 일 | 사람이 하는 일 |
|---|---|---|---|
| 시작 `/fa-init` | Claude Code·Codex 세션 | 저장소·칸반·라벨·이슈 템플릿·설정을 만든다 | 한 번 부른다 (프로젝트당 1회) |
| 주문 `/fa-order` | Claude Code·Codex 세션 | 인터뷰를 하고 이슈로 쪼갠다 | 인터뷰에 답하고 한 번 승인한다 |
| 실행 `fa gate` | 백그라운드 프로그램 (LLM 없음) | 승인된 이슈를 처리한다: 구현 → 확인 → 리뷰 → PR → 머지 → 다음 이슈 | 없음. 사람이 자리에 없어도 돈다 |
| 검수 `/fa-inspect` | Claude Code·Codex 세션 | 사람이 볼 것을 행동 순서로 묶어 묻는다 | 돌아와서 답한다 (다음 주문의 입력) |

대화가 필요한 단계(시작·주문·검수)는 사람이 켠 세션이 진행하고 끝나면 닫힌다. 일을 굴리는 쪽은 gate 하나다. 상태는 모두 GitHub에 있어서 어떤 세션이 언제 켜져도 이어서 한다. 시간대는 정해져 있지 않다(`run_hours`로 제한할 수 있다).

설치는 두 조각: 대화 단계용 스킬(`fa-init`, `fa-order`, `fa-inspect`)과 명령줄 프로그램 `foragent`(별칭 `fa`: `fa gate`, `fa config`).

기존 레인에 사람이 고른 고정 스킬을 지정하려면 `fa lanes skills <lane> <id>...`를 쓴다.
지정한 목록으로 `.foragent.yml`과 해당 에이전트의 스킬 목록을 교체하고, 스킬은 로컬에 설치한다.
ID 없이 `fa lanes skills <lane>`을 실행하면 목록을 비운다. `style` 스킬은 레인당 하나까지 지정할 수 있다.
변경은 작업 트리에 남으므로 확인한 뒤 커밋한다.
`.foragent.yml` 수정은 커밋해서 기본 브랜치에 푸시해야 gate tick에 반영된다.

## 문서

- [docs/decisions.md](docs/decisions.md) — 확정된 결정과 이유
- [docs/status.md](docs/status.md) — 무엇이 설계됐고 무엇이 남았나
- [docs/order.md](docs/order.md) · [docs/gate.md](docs/gate.md) · [docs/inspect.md](docs/inspect.md) — 단계별 설계
- [docs/contract.md](docs/contract.md) — order ↔ gate 경계 계약 (gate 쪽 답신, 확정본)
- [docs/contract-proposal.md](docs/contract-proposal.md) — 경계 계약 첫 제안 (order 쪽)
- [docs/research/](docs/research/) — 비슷한 도구 조사

<!-- tagline candidates (2026-10-20):
Say what you want. Agents build it while you're away, and show you exactly what to check.
Multi-agent lanes, isolated worktrees and task graphs — without setting up any of it.
-->

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option. Unless you explicitly state otherwise, any contribution you intentionally submit for inclusion in this work, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or conditions.
