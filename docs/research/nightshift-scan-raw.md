scan:  /Users/apple/.cxdo/20261020-114101.scan.jsonl
final message:
FINDINGS
O=repos/orwa-nightshift/plugins/nightshift,M=repos/marcus-nightshift/internal/orchestrator
O 1. 개발자 장기작업 플러그인(../../README.md:13)
M 1. 개발자 유지보수CLI(../../README.md:11)
O 12. Bash/PS·MIT·plugin(README.md:20,30),Bats(../../tests/ns.bats:1); 5.8만줄(plugins 스크립트 합산)
M 12. Go·brew·MIT(../../README.md:27,35,293),테스트(../../Makefile:22); 2.6만줄(cmd/internal 비test Go 합산)
O/M 13. 2~9 인용파일이 후보

|번호|O|M|
|--|--|--|
|2|수동·OS설정 출력;셸(runtime/schedule.sh:436)|run/daemon·cron;Go(../scheduler/scheduler.go:203)|
|3|지정이슈→로컬(runtime/import-issues.sh:240);첫 미완료(runtime/punch-list.sh:4)|유형점수/쿨다운(../tasks/selector.go:209);이슈본문 미연결(../../cmd/nightshift/commands/run.go:719)|
|4|열린박스0(hooks/clock-out-gate.sh:344)|동일CLI 리뷰passed(orchestrator.go:616)|
|5|lease·중복방지(lib/ownership.sh:335)|순차·branch지침(orchestrator.go:696,772)|
|6|기본로컬·허가시push(skills/nightshift/SKILL.md:119)|README 전부PR(../../README.md:19)↔PR없이완료(orchestrator.go:284)|
|7|기한·stall(hooks/clock-out-gate.sh:358,366)|30분·3회,실패/포기(orchestrator.go:25,253,320)|
|8|호스트계측·시간/토큰한도(lib/budget.sh:187)|잔량예산(../budget/budget.go:199);추정실적(../../cmd/nightshift/commands/run.go:775)|
|9|아침MD·명령알림(hooks/clock-out-gate.sh:110,159)|MD/JSON저장·발송미연결(../../cmd/nightshift/commands/run_reporting.go:55)|
|10|대화→로컬draft(skills/plan/SKILL.md:130)|없음(확인:../tasks/register.go:14)|
|11|claude --resume I -p P(runtime/claude/watchman.sh:249);codex exec resume I P(runtime/codex/watchman.sh:278);Cursor agent --resume=I -p --workspace W P(runtime/cursor/watchman.sh:257)|claude --print --dangerously-skip-permissions P(../agents/claude.go:130);codex exec --dangerously-bypass-approvals-and-sandbox P(../agents/codex.go:87);gh copilot -- -p P --no-ask-user --silent(../agents/copilot.go:100)|

COVERAGE
본문·발췌 O110/M74파일. 저장소 기준 O:plugins/nightshift(하위포함),docs/tests/루트/.github; M:cmd/internal/docs/website/docs/루트/.github. 미정독코드·M테스트는 검색만; O의 나머지테스트·예제·그림·.git·배포·미배정문서는 범위밖 미독. 실행·변경 없음.

UNCERTAIN
실행 검증 미수행. 의존성·테스트·브랜치 관련 모델 지침의 실제 준수와 결과 품질은 파일로 확정 못함. worktree/자동머지미확정.