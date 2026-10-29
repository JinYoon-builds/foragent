# Foragent

> Your agent forages for the next task.

Stock Claude Code is already good at finishing one task. Foragent automates the part **between tasks**: when one task ends, it runs the done-when commands, sends the change to an adversarial review, merges it when it passes, and picks up the next issue by itself. You decide what to build and approve it once.

```
task ends → done-when commands → adversarial review → merge → next issue
```

<!-- GIF placeholder: lane view and statusline recording, coming soon (Jin records it). -->
**GIF placeholder:** a recording of the lane view and the statusline is coming soon.

Foragent built itself: 208 merged PRs in 10 days (counted from `git log` on 2026-10-29).

[한국어 README](#한국어)

## Install

```sh
uv tool install git+https://github.com/JinYoon-builds/foragent
fa init
```

Foragent needs Python 3.12 or newer. You do not have to install it first: if no suitable Python is on your machine, uv fetches one automatically and installs the `fa` command with it. Run `fa init` inside the project folder you want Foragent to work on.

At the end, `fa init` offers one optional starter order: a tiny task (about 10 minutes) that shows the whole flow from order to approval to a merged PR. Nothing is created then. To try it, type `/fa` and say `starter: add a --version flag`, or name another tiny task from your project.

If Claude Code runs on `ANTHROPIC_API_KEY`, it bills the API. The gate starts it only under a daily cap, so add one to `.foragent.yml`:

```yaml
executors: [{name: claude, quota: api, spend_cap: <USD per day>}]
```

Then open Claude Code in the project and type `/fa`. It sets up the project the first time, and later it briefs you and takes your next order.

## Lanes

A lane is an agent that owns one area of your code, for example `src/foragent/order.py` and its tests. Issues are split by lane, so each lane works in its own area.

- **Own an area.** Each lane has its own paths. An issue goes to the lane that owns the files it changes, so two lanes do not edit the same files.
- **Learn.** Each lane keeps its own memory of patterns, pitfalls and commands in that area. Lessons from one issue help the next issue in the same lane.
- **Work in parallel.** Lanes run at the same time, each in its own git worktree. The gate merges the results one by one.

## Runs on stock Claude Code

Foragent does not replace Claude Code. It uses what stock Claude Code already has:

- **Lanes are `.claude/agents`.** Each lane is a plain agent file, `.claude/agents/fa-<lane>.md`.
- **Skills are `SKILL.md`.** `/fa`, `/fa-order` and `/fa-inspect` are ordinary skills with a `SKILL.md` file.
- **Memory is project memory.** Each lane writes its lessons to Claude Code project memory in the repository.
- **Only the gate runs outside.** `fa gate` is a background program with no LLM of its own. It runs outside Claude Code and starts a coding session for each issue.

Codex CLI is optional. If Codex CLI is logged in, the gate starts the coding session with Codex CLI and Claude Code reviews the change. Otherwise Claude Code implements it and a separate Claude Code session reviews it.

## Known limits, contributions welcome

These limits are known. Each item names the file where a change would start.

- **Codex-only setups are not supported yet.** Foragent runs on Claude Code; Codex CLI is optional. Running with Codex CLI and without Claude Code is not supported at launch. Start at `config.resolved_reviewer` (the default reviewer) and `orch_spawn` (`src/foragent/orch_spawn.py`, which starts the orchestrator with `claude`).
- **The background gate is macOS only.** It runs as a launchd service. A Linux service (for example systemd) is not written yet. Start at `src/foragent/launchd.py`.
- **Native Windows is not supported yet.** WSL will follow the Linux gate when it exists. Start at `src/foragent/launchd.py`.
- **Terminal switching works only in Orca.** Foragent opens the lane view beside your session in tmux, WezTerm, kitty, iTerm2 or Terminal, but only Orca lets it switch back to the terminal it opened before. Other terminals open a new split instead. Start at `src/foragent/watch_open.py`.
- **Update checks always use the main repository.** A fork still checks `JinYoon-builds/foragent` for updates and installs from it. Start at `src/foragent/update_check.py`.

## 한국어

순정 Claude Code는 이미 작업 하나를 잘 끝내요. Foragent는 **작업과 작업 사이**를 자동화해요. 작업이 끝나면 완료 확인 명령을 돌리고, 변경을 적대적 리뷰에 보내고, 통과하면 합치고, 다음 이슈를 스스로 가져와요. 사람은 무엇을 만들지 정하고 한 번 승인해요.

```
작업이 끝남 → 완료 확인 명령 → 적대적 리뷰 → 머지 → 다음 이슈
```

### 설치

```sh
uv tool install git+https://github.com/JinYoon-builds/foragent
fa init
```

Foragent는 Python 3.12 이상이 필요해요. 먼저 설치하지 않아도 돼요. 알맞은 Python이 없으면 uv가 자동으로 받아서 `fa` 명령을 함께 설치해요. `fa init`은 Foragent가 일할 프로젝트 폴더 안에서 실행해요.

마지막에 `fa init`이 선택 사항인 starter 주문 하나를 제안해요. 약 10분짜리 작은 일로 주문, 승인, PR 병합까지 전체 흐름을 보여 줘요. 이때는 아무것도 만들지 않아요. 해 보려면 `/fa`를 입력하고 `starter: --version 플래그 추가`라고 말해요. 프로젝트의 다른 작은 일을 말해도 돼요.

Claude Code가 `ANTHROPIC_API_KEY`로 돌면 API 비용이 나가요. gate는 하루 상한이 있을 때만 Claude Code를 시작해요. `.foragent.yml`에 상한을 넣어요.

```yaml
executors: [{name: claude, quota: api, spend_cap: <하루 USD>}]
```

그다음 프로젝트에서 Claude Code를 열고 `/fa`를 입력해요. 처음에는 프로젝트를 설정하고, 그 뒤로는 상황을 알려 주고 다음 주문을 받아요.

### 레인

레인은 코드의 한 영역을 맡는 에이전트예요. 예를 들어 `src/foragent/order.py`와 그 시험을 맡아요. 이슈는 레인별로 나뉘어서 레인마다 자기 영역에서 일해요.

- **영역을 맡아요.** 레인마다 자기 경로가 있어요. 이슈는 고치는 파일을 맡은 레인에 가요. 그래서 두 레인이 같은 파일을 고치지 않아요.
- **배워요.** 레인마다 그 영역의 패턴, 함정, 명령을 기억해요. 한 이슈에서 배운 것이 같은 레인의 다음 이슈를 도와요.
- **동시에 일해요.** 레인은 각자 자기 git worktree에서 동시에 돌아요. gate가 결과를 하나씩 합쳐요.

### 순정 Claude Code 위에서 돌아요

Foragent는 Claude Code를 대신하지 않아요. 순정 Claude Code에 이미 있는 것을 써요.

- **레인은 `.claude/agents`예요.** 레인마다 평범한 에이전트 파일 `.claude/agents/fa-<lane>.md`가 있어요.
- **스킬은 `SKILL.md`예요.** `/fa`, `/fa-order`, `/fa-inspect`는 `SKILL.md` 파일이 있는 평범한 스킬이에요.
- **기억은 프로젝트 메모리예요.** 레인마다 배운 것을 저장소의 Claude Code 프로젝트 메모리에 써요.
- **gate만 밖에서 돌아요.** `fa gate`는 자체 LLM이 없는 백그라운드 프로그램이에요. Claude Code 밖에서 돌면서 이슈마다 코딩 세션을 시작해요.

Codex CLI는 선택이에요. Codex CLI에 로그인되어 있으면 gate가 Codex CLI로 코딩 세션을 시작하고 Claude Code가 변경을 리뷰해요. 아니면 Claude Code가 구현하고 별도의 Claude Code 세션이 리뷰해요.

### 알려진 한계, 기여 환영

알고 있는 한계예요. 항목마다 고칠 때 시작할 파일을 적었어요.

- **Codex만으로는 아직 못 써요.** Foragent는 Claude Code 위에서 돌고 Codex CLI는 선택이에요. Claude Code 없이 Codex CLI만으로 돌리는 것은 출시 때 지원하지 않아요. `config.resolved_reviewer`(기본 리뷰어)와 `orch_spawn`(`src/foragent/orch_spawn.py`, `claude`로 오케스트레이터를 시작해요)에서 시작해요.
- **백그라운드 gate는 macOS에서만 돌아요.** launchd 서비스로 돌아요. Linux 서비스(예: systemd)는 아직 없어요. `src/foragent/launchd.py`에서 시작해요.
- **Windows는 아직 지원하지 않아요.** WSL은 Linux gate가 생기면 따라가요. `src/foragent/launchd.py`에서 시작해요.
- **터미널 전환은 Orca에서만 돼요.** Foragent는 tmux, WezTerm, kitty, iTerm2, Terminal에서 세션 옆에 레인 화면을 열어요. 하지만 전에 열었던 터미널로 돌아가는 것은 Orca에서만 돼요. 다른 터미널은 새 분할 창을 열어요. `src/foragent/watch_open.py`에서 시작해요.
- **업데이트 확인은 늘 원래 저장소를 봐요.** 포크도 `JinYoon-builds/foragent`에서 업데이트를 확인하고 설치해요. `src/foragent/update_check.py`에서 시작해요.

### 문서

- [docs/decisions.md](docs/decisions.md) — 확정된 결정과 이유
- [docs/status.md](docs/status.md) — 무엇이 설계됐고 무엇이 남았나
- [docs/order.md](docs/order.md) · [docs/gate.md](docs/gate.md) · [docs/inspect.md](docs/inspect.md) — 단계별 설계
- [docs/contract.md](docs/contract.md) — order ↔ gate 경계 계약 (gate 쪽 답신, 확정본)
- [docs/research/](docs/research/) — 비슷한 도구 조사

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE))
- MIT license ([LICENSE-MIT](LICENSE-MIT))

at your option. Unless you explicitly state otherwise, any contribution you intentionally submit for inclusion in this work, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or conditions.
