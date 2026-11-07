<!-- foragent:v1 -->
## Goal
Add a `tally words FILE` command that prints the number of whitespace-separated words in the file.

## Out of scope
- Any other command or option.

## Done when
```sh
uv run pytest -q tests/acceptance/test_a_words.py
```
Acceptance test: [`tests/acceptance/test_a_words.py`](../blob/main/tests/acceptance/test_a_words.py)
