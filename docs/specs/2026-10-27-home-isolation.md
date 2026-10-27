# Home isolation (2026-10-27)

On 2026-10-27 an implementer's attempt wrote upstream_reports: false into the real ~/.config/foragent/config.yml, because test_board_projects.py and test_view_open_triggers.py run a real fa init without isolating HOME. This is a safety line: tests must fail closed.
