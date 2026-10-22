# skills 레인 현재 메모

- 라이브러리 스킬 설치 기본값은 로컬 복사다. `.gitignore`에는 `/.claude/skills/<이름>/` 규칙만 한 번 추가하며, 기존 내용은 바이트 그대로 보존한다. 전체 `.claude/skills/`를 제외하면 사용자 스킬도 빠지므로 개별 경로를 쓴다.
- `SkillManager.install(..., commit=True)` / `fa skills install --commit`에서만 기존 오픈소스 라이선스 차단과 `THIRD_PARTY_NOTICES.md` 생성을 적용한다. 로컬 설치는 불명확하거나 비공개인 라이선스도 경고 후 허용하며 Git 조회가 필요 없다.
- `lane_files()`는 레인 설정·에이전트 정의·`.gitignore`만 반환한다. `order.publish()`의 중단 복구도 이 함수를 쓰므로 라이브러리 스킬 파일을 반환하면 강제 추적·커밋된다.
- runner는 작업공간 생성 직후 로컬 설치본 → 사용 가능한 라이브러리 캐시 순서로 복사한다. 기존 작업공간의 스킬은 보존한다. 대상 브랜치 변경·패치 충돌 복구로 작업공간을 다시 만들 때도 복사가 필요하다.
- 복사한 경로만 `GitRepo.local_skill_paths`에 등록한다. 자동 스테이징에서 제외하고 실행기가 강제로 스테이징한 복사본도 커밋 전에 인덱스에서 제거한다. 사용자 스킬은 계속 커밋할 수 있다.
- 검증 명령: `uv run pytest -q tests/acceptance/test_skills_local.py`. 관련 회귀: `tests/test_skills_lanes.py`, `tests/test_git.py`, `tests/test_runner.py`, `tests/test_cli.py`, `tests/test_order.py`. acceptance 파일은 수정하지 않는다.
- 제한 환경에서 기본 uv 캐시에 쓰기가 막히면 `/private/tmp`의 작업 전용 캐시에 필요한 캐시를 복사한 뒤 `UV_CACHE_DIR=/private/tmp/fa-issue-39-uv-cache uv run --offline ...`로 검증할 수 있다.
- `review_scope` 기본값은 `claude-local`이다. `pr-body`는 각 리뷰 전에 검증한 변경을 커밋·푸시하고 같은 PR을 갱신한 뒤 Claude에 `review_pr`를 전달한다. CLI 명령은 `/code-review <PR 번호> <review_level>`이며 임시 `CLAUDE.local.md`를 만들지 않는다.
- PR 본문은 초기 생성·재리뷰·최종 공개 모두 같은 렌더러를 쓴다. `pr-body`에서는 범위 규칙과 판정만 본문에, 각 라운드 지적·메모는 PR 댓글에 기록한다. 기본 모드는 이전처럼 본문에 리뷰 이력을 남긴다.
- 리뷰 모드 검증: `uv run pytest -q tests/acceptance/test_skills_review_scope.py`. 회귀: `tests/test_config.py tests/test_executors.py tests/test_runner.py tests/test_git.py`. `tests/test_runner.py -k pr_body_reviews`는 매 라운드의 로컬 HEAD·원격 브랜치·PR HEAD 일치와 재시도 댓글 보존을 확인한다.
- `SkillManager.skills()`는 등록된 라이브러리가 디스크에 없으면 복제하고 조회를 계속한다. catalog·install·레인 검증이 이 경로를 공유한다. 기존 디렉터리는 갱신하지 않으며, runner의 `available_only=True` 캐시 조회는 누락 라이브러리를 건너뛰어 오프라인 동작을 보존한다. 복제 실패는 라이브러리 이름을 포함한 `SkillError`로 전달한다.
- 자동 복제 검증: `uv run pytest tests/acceptance/test_skills_autosync.py`. `tests/test_skills_lanes.py`의 자동 복제 회귀는 로컬 bare 저장소로 복제 1회·기존 캐시 미갱신·레인 설치를 함께 확인한다.
