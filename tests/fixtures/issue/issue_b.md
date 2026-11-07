<!-- foragent:v1 -->
## Goal
Add a `tally lines FILE` command that prints the number of lines in the file.

## Out of scope
- Changing the words command.

## Done when
```sh
uv run pytest -q tests/acceptance/test_b_lines.py
```
```bash
uv run pytest -q tests/acceptance/test_b_cli.py
```
- [ ] The help text explains the lines command.
* [X] Empty files are covered by an acceptance test.
Acceptance tests: `tests/acceptance/test_b_lines.py`, `tests/acceptance/test_b_cli.py`.
