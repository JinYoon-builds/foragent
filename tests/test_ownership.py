from types import SimpleNamespace
import json

import pytest

from foragent import identity_cache
from foragent.config import Config
from foragent.ownership import OwnershipError, effective_orderer, owner_label, parse_orderer, require_owned
from foragent.tracker import FakeTracker, GhTracker, IdentityError


def issue(*labels, number=12):
    return SimpleNamespace(number=number, labels=frozenset(labels))


def test_parse_orderer_reads_one_normalized_label():
    assert parse_orderer(["size:s", "By:Alice"]) == "alice"
    assert parse_orderer(["size:s"]) is None
    assert parse_orderer(["by:alice", "by:ALICE"]) == "alice"
    with pytest.raises(OwnershipError, match="alice, bob"):
        parse_orderer(["by:alice", "by:bob"])
    with pytest.raises(OwnershipError):
        parse_orderer(["by:"])
    assert owner_label("Alice") == "by:alice"


def test_effective_orderer_uses_legacy_owner_only_for_unlabelled_issues():
    team = Config(legacy_owner="Jin")
    assert effective_orderer(issue("by:bob"), team, "alice") == "bob"
    assert effective_orderer(issue(), team, "alice") == "jin"
    assert effective_orderer(issue(), Config(), "Alice") == "alice"  # The unchanged singleton.
    with pytest.raises(OwnershipError):
        effective_orderer(issue(), Config(), None)


def test_require_owned_refuses_foreign_and_unknown_factories():
    require_owned(issue("by:alice"), Config(), "ALICE")
    require_owned(issue(), Config(legacy_owner="alice"), "alice")
    with pytest.raises(OwnershipError, match=r"^#12 is worked by bob; this factory is alice\.$"):
        require_owned(issue("by:bob"), Config(), "alice")
    with pytest.raises(OwnershipError, match="worked by jin"):
        require_owned(issue(), Config(legacy_owner="jin"), "alice")
    with pytest.raises(OwnershipError, match="fa init"):
        require_owned(issue("by:alice"), Config(), None)
    with pytest.raises(OwnershipError, match="#12은 bob의 일이에요"):
        require_owned(issue("by:bob"), Config(language="ko"), "alice")


def fake_gh(answers, calls):
    def run(args, input=None):
        calls.append(args)
        assert args == ["api", "user"]
        return json.dumps(answers[0])
    return run


def test_signed_in_account_switch_misses_the_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    (tmp_path / "gh").mkdir()
    hosts = tmp_path / "gh" / "hosts.yml"
    answers, calls = [{"login": "Alice"}], []
    run = fake_gh(answers, calls)
    # No readable credential context: never cached, never reused.
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    assert len(calls) == 2
    hosts.write_text("github.com:\n    user: alice\n")
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    assert len(calls) == 3
    hosts.write_text("github.com:\n    user: bob\n")  # gh auth switch
    answers[0] = {"login": "Bob"}
    assert GhTracker("o/r", run=run).authenticated_login() == "bob"
    assert len(calls) == 4
    # fa init reads again even when the cache answers.
    assert GhTracker("o/r", run=run).authenticated_login(refresh=True) == "bob"
    assert len(calls) == 5
    assert "bob" not in identity_cache._path().read_text().replace('"bob"', "")  # Only hashes as keys.


def test_app_credentials_cannot_order_as_a_person(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-token")
    run = fake_gh([{"login": "foragent[bot]", "type": "Bot"}], [])
    with pytest.raises(IdentityError, match="not a person"):
        GhTracker("o/r", run=run).authenticated_login()


def test_fake_tracker_login_is_injectable():
    assert FakeTracker(actor="Reviewer").authenticated_login() == "reviewer"
    assert FakeTracker(actor="reviewer", login="Alice").authenticated_login() == "alice"


@pytest.mark.parametrize("host, variable", [
    (None, "GH_TOKEN"), (None, "GITHUB_TOKEN"), ("acme.ghe.com", "GH_TOKEN"),
    ("github.localhost", "GITHUB_TOKEN"), ("git.example.com", "GH_ENTERPRISE_TOKEN"),
    ("git.example.com", "GITHUB_ENTERPRISE_TOKEN"), ("acme.ghe.com", "GH_ENTERPRISE_TOKEN"),
])
def test_any_token_switch_reads_the_login_again(monkeypatch, tmp_path, host, variable):
    for name in identity_cache.TOKEN_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    if host:
        monkeypatch.setenv("GH_HOST", host)
    else:
        monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hosts.yml").write_text("github.com:\n    user: alice\n")  # Unchanged throughout.
    answers, calls = [{"login": "Alice"}], []
    run = fake_gh(answers, calls)
    monkeypatch.setenv(variable, "alice-credential")
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    answers[0] = {"login": "Bob"}
    monkeypatch.setenv(variable, "bob-credential")
    assert GhTracker("o/r", run=run).authenticated_login() == "bob"
    assert len(calls) == 2


def test_host_switch_reads_the_login_again(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hosts.yml").write_text("github.com:\n    user: alice\ngit.example.com:\n    user: carol\n")
    for name in ("GH_HOST", *identity_cache.TOKEN_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    answers, calls = [{"login": "Alice"}], []
    run = fake_gh(answers, calls)
    assert GhTracker("o/r", run=run).authenticated_login() == "alice"
    monkeypatch.setenv("GH_HOST", "git.example.com")
    answers[0] = {"login": "Carol"}
    assert GhTracker("o/r", run=run).authenticated_login() == "carol"
    assert len(calls) == 2
