# draft, not posted

**제목:** Foragent – 작업이 끝나면 Claude Code가 다음 이슈를 스스로 가져오는 도구

**링크:** https://github.com/JinYoon-builds/foragent

- 한 작업을 끝까지 하는 것은 이제 순정 Claude Code도 잘해요. Foragent는 **작업과 작업 사이**를 자동화해요.
  - 작업이 끝나면 완료 확인 명령을 돌리고, 적대적 리뷰를 거쳐, 통과하면 합치고, 다음 이슈를 가져와요.
  - 사람은 무엇을 만들지 정하고 한 번 승인해요. "다 했어? → 리뷰해 → 다음 거"를 손으로 반복하지 않아요.
- **주문 `/fa-order`**: 인터뷰를 하고, 실행할 수 있는 인수 시험이 붙은 GitHub 이슈로 쪼개요.
- **실행 `fa gate`**: LLM이 없는 백그라운드 프로그램이에요. 승인된 이슈마다 코딩 세션(Codex CLI에 로그인되어 있으면 Codex CLI, 아니면 Claude Code)을 띄워 구현 → 확인 → 다른 도구의 리뷰 → PR → 머지를 해요. 사람이 자리에 없어도 돌아요.
- **레인**: 코드의 한 영역을 맡는 에이전트예요.
  - 레인마다 자기 경로가 있어서 두 레인이 같은 파일을 고치지 않아요.
  - 레인마다 그 영역의 기억(패턴·함정·명령)을 쌓아서 다음 이슈에 써요.
  - 레인마다 자기 git worktree에서 동시에 일하고, gate가 결과를 하나씩 합쳐요.
- **순정 Claude Code 위에서 돌아요**: 레인은 `.claude/agents`, 스킬은 `SKILL.md`, 기억은 프로젝트 메모리예요. Codex CLI는 선택이에요. 로그인되어 있으면 Codex CLI가 구현하고 Claude Code가 리뷰해요.
- Foragent는 자기 자신을 만들었어요. 10일 동안 PR 208개를 합쳤어요(2026-10-29 `git log` 기준).
- **알려진 한계** (README에 고칠 때 시작할 파일이 적혀 있어요. 기여를 환영해요)
  - Claude Code가 필요해요. Codex만으로는 아직 못 써요.
  - 백그라운드 gate는 macOS에서만 돌아요. Linux 서비스는 아직 없어요.
  - Windows는 아직 지원하지 않아요.
  - 전에 열었던 터미널로 돌아가는 기능은 Orca에서만 돼요.
  - 업데이트 확인은 포크에서도 원래 저장소를 봐요.
- 설치: `uv tool install git+https://github.com/JinYoon-builds/foragent` 뒤 프로젝트 폴더에서 `fa init`, Claude Code에서 `/fa`
