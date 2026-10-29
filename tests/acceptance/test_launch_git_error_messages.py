"""Launch conflict diagnostics follow project language and the messages registry."""

import re

import pytest
import yaml

from test_skills_autosync import isolated_home


@pytest.mark.parametrize("language", ["en", "ko"])
def test_conflict_error_uses_config_language_and_registry(tmp_path, monkeypatch, language):
    from foragent import i18n
    from foragent.git import GitRepo, PublicationError

    (tmp_path / ".foragent.yml").write_text(yaml.safe_dump({"language": language}), encoding="utf-8")
    markers = [("src/example.py", 4), ("other.txt", 12)]
    git = GitRepo(tmp_path)
    monkeypatch.setattr(git, "changed_paths", lambda base: [path for path, _ in markers])
    monkeypatch.setattr(git, "conflict_marker_lines", lambda paths: markers)

    # Discover the implementation's key by observing the public registry, rather
    # than fixing a new key name or requiring a new exception signature.
    looked_up = []

    class Catalogue(dict):
        def __getitem__(self, key):
            looked_up.append(key)
            return f"REGISTRY-{language}: " + super().__getitem__(key)

    monkeypatch.setitem(i18n.MESSAGES, language, Catalogue(i18n.MESSAGES[language]))
    with pytest.raises(PublicationError) as raised:
        git.check_publication("offline-base-sha")
    error = raised.value
    message = str(error)
    assert error.markers == markers
    assert all(f"{path}:{line}" in message for path, line in markers)
    assert looked_up and f"REGISTRY-{language}: " in message, (
        f"Conflict text must come from the {language} registry chosen by .foragent.yml: {message!r}")
    assert all(key in i18n.MESSAGES[other] for key in looked_up for other in ("en", "ko"))
    if language == "en":
        assert "conflict" in message.lower() and not re.search("[가-힣]", message)
    else:
        assert re.search("[가-힣]", message), "Korean configuration needs a Korean diagnostic"
