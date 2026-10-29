# draft, not posted

**제목:** Show HN: Foragent – Claude Code가 다음 이슈를 스스로 가져와요

**URL:** https://github.com/JinYoon-builds/foragent

**본문:**

(HN은 영어 게시판이에요. 이 파일은 영어 글의 내용을 한국어로 옮긴 검토용 초안이에요. 실제 게시는 hn.en.md를 써요.)

한 작업을 끝까지 하는 것은 이제 순정 Claude Code도 잘해요. 제가 계속 손으로 하던 일은 작업과 작업 사이였어요. "다 했어? 확인 돌려, 리뷰해, 합쳐, 이제 다음 거."

Foragent는 이 반복을 자동화해요.

    작업이 끝남 → 완료 확인 명령 → 적대적 리뷰 → 머지 → 다음 이슈

`/fa-order`에서 만들 것을 말하면 인터뷰를 하고, 실행할 수 있는 인수 시험이 붙은 GitHub 이슈로 쪼개고, 한 번 승인을 받아요. 그 뒤로는 `fa gate`(LLM이 없는 백그라운드 프로그램)가 이슈마다 코딩 세션(Codex CLI에 로그인되어 있으면 Codex CLI, 아니면 Claude Code)을 띄우고, 완료 확인 명령을 돌리고, 다른 도구의 적대적 리뷰를 거쳐 PR을 열고, 통과하면 합치고 다음으로 넘어가요.

일은 레인으로 나눠요. 레인은 코드의 한 영역을 맡는 에이전트예요. 레인마다 자기 경로, 그 영역의 기억, 자기 git worktree가 있어서 같은 파일을 건드리지 않고 동시에 일해요.

순정 Claude Code 위에서 돌아요. 레인은 `.claude/agents` 파일, 스킬은 `SKILL.md`, 기억은 프로젝트 메모리예요. Codex CLI는 선택이에요. 로그인되어 있으면 Codex CLI가 구현하고 Claude Code가 리뷰해요. 없으면 Claude Code가 구현하고 별도의 Claude Code 세션이 리뷰해요.

Foragent는 자기 자신을 만들었어요. 10일 동안 PR 208개를 합쳤어요(2026-10-29 `git log` 기준).

알려진 한계:

- Claude Code가 필요해요. Codex만으로는 아직 못 써요.
- 백그라운드 gate는 macOS(launchd)에서만 돌아요. Linux 서비스는 아직 없어요.
- Windows는 아직 지원하지 않아요. WSL은 Linux gate가 생기면 따라가요.
- 전에 열었던 터미널로 돌아가는 기능은 Orca에서만 돼요. 다른 터미널은 새 분할 창을 열어요.
- 업데이트 확인은 포크에서도 원래 저장소를 봐요.

README의 각 한계에는 고칠 때 시작할 파일이 적혀 있어요. 기여를 환영해요.

설치:

    uv tool install git+https://github.com/JinYoon-builds/foragent
    fa init

그다음 Claude Code에서 `/fa`를 입력해요.
