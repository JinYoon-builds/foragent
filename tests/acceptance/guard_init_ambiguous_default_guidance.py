"""Regression guard for n66-ambiguous-skill-guidance; not part of Done when (passes on main).

fa init replaces only a default copy it installed itself: a copy the person placed under
.claude/skills stays byte-for-byte after the role's default_skills entry switches to another
matching qualified id and fa init runs again.
"""

import yaml

from test_setup_checklist_tools import machine  # noqa: F401 -- fixture
from test_init_picks_ambiguous_skill import INIT_FLAGS, PROVE, UNIQUE, add_libraries, run
from test_init_ambiguous_default_guidance import workers_only

from foragent.git import GitRepo


def test_a_copy_the_person_placed_is_not_replaced(machine, capsys, tmp_path, monkeypatch):  # noqa: F811
    add_libraries(machine, tmp_path)
    workers_only(machine)
    monkeypatch.setattr(GitRepo, "commit_paths", lambda *args, **kwargs: None)
    own = machine.root / ".claude/skills" / PROVE / "SKILL.md"
    own.parent.mkdir(parents=True)
    mine = f"---\nname: {PROVE}\ndescription: my own\n---\nMy own copy.\n"
    own.write_text(mine, encoding="utf-8")
    argv = ["init", "--repo-root", str(machine.root), "--language", "en", *INIT_FLAGS]

    code, output = run(argv, capsys)
    assert code == 0, output
    project = machine.root / ".foragent.yml"
    written = yaml.safe_load(project.read_text(encoding="utf-8"))
    written.setdefault("default_skills", {})["workers"] = ["zeta-skills:" + PROVE, UNIQUE]
    project.write_text(yaml.safe_dump(written, sort_keys=False), encoding="utf-8")

    code, output = run(argv, capsys)
    assert "Traceback" not in output, output
    assert code == 0, output
    assert own.read_text(encoding="utf-8") == mine, "A copy fa init did not install stays as the person left it"
