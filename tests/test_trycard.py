import pytest

from foragent.trycard import TryCard, WebSpec, parse_try, pr_try_text


CLI_CARD = """## What changed
합계 보고서를 볼 수 있습니다.
## Try it
kind: cli
```sh
printf '합계: 3\\n'
```
```sh
printf '완료\\n'
```
## Expect
합계: 3과 완료가 표시됩니다.
"""

WEB_CARD = """## What changed
이름으로 인사합니다.
## Try it
kind: web
## Expect
안녕하세요, 진! 이 보입니다.
## Web
start: python app.py --host 0.0.0.0
port: 8765
path: /hello
steps:
- 이름에 진 입력
- 인사 버튼 클릭
"""


def test_cli_card_korean_and_crlf():
    assert parse_try(CLI_CARD.replace("\n", "\r\n")) == TryCard(
        "합계 보고서를 볼 수 있습니다.", "cli", ["printf '합계: 3\\n'", "printf '완료\\n'"],
        "합계: 3과 완료가 표시됩니다.", None,
    )


def test_web_card_korean():
    card = parse_try(WEB_CARD)
    assert card.kind == "web" and card.commands == []
    assert card.web == WebSpec("python app.py --host 0.0.0.0", 8765, "/hello", ["이름에 진 입력", "인사 버튼 클릭"])
    assert card.expect == "안녕하세요, 진! 이 보입니다."


@pytest.mark.parametrize("text", ["", "일반 설명", "## Try it\nkind: unknown\n```sh\n```"])
def test_missing_parts_and_unknown_kind(text):
    assert parse_try(text) == TryCard("", "cli", [], "", None)


@pytest.mark.parametrize("port", ["", "invalid"])
def test_partial_web_card(port):
    card = parse_try(f"## Try it\nkind: web\n## Web\nport: {port}\n")
    assert card.web == WebSpec("", 0, "", [])


def test_fenced_script_stays_one_command_and_ignores_headings():
    script = "for n in 1 2; do\n  echo $n\ndone\n## Expect\necho real"
    card = parse_try(f"## Try it ###\nkind: unknown\n~~~~sh\n{script}\n~~~~\n## Expect\n두 줄")
    assert card.commands == [script] and card.expect == "두 줄" and card.kind == "cli"


def test_pr_wrapper_recovers_complete_card_and_ignores_fenced_fake_section():
    body = "## Review\n```md\n## Try it\nignore\n```\n\n## Try it\n\n" + CLI_CARD
    assert pr_try_text(body) == "\n" + CLI_CARD
    assert parse_try(pr_try_text(body)) == parse_try(CLI_CARD)
    assert pr_try_text("Closes #1\n## Review\nPASS") is None
