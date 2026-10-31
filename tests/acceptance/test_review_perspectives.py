"""Acceptance test for review-perspectives. Locked by Foragent; do not edit."""

from dataclasses import asdict, dataclass, field
from importlib import import_module
import socket

import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.runner import run_issue
from runner_support import enable_reviewer
from test_risk_map import copy_history, entry, offline, risk_case
from test_skills_autosync import isolated_home


NAMES = ("goal-fidelity", "concurrency", "interruption-rollback", "test-quality")
THEMES = (
    ("goal", "acceptance", "out of scope"),
    ("race", "ordering", "stale"),
    ("interrupt", "rollback", "partial"),
    ("test", "wrong value", "skip", "twice"),
)
# Project overrides give prompt assertions distinct, known focus texts without
# importing the missing API during collection or deriving the oracle from it.
CHECKLIST = [
    {"name": name, "focus": f"FOCUS-{name}: " + ", ".join(themes)}
    for name, themes in zip(NAMES, THEMES)
]
PROJECT = {"name": "observability", "focus": "FOCUS-observability: lost diagnostic context"}


@pytest.fixture(autouse=True)
def review_offline(monkeypatch, offline):
    from foragent.executors import ClaudeExecutor, CodexExecutor

    def forbidden(*args, **kwargs):
        pytest.fail("Perspective acceptance must use fake executors and no network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(ClaudeExecutor, "run", forbidden)
    monkeypatch.setattr(CodexExecutor, "run", forbidden)


@dataclass(frozen=True)
class ReviewConfig(Config):
    # YAML validation is tested separately. Supply the proposed public fields to
    # behavioral tests so today's failures expose missing review behavior rather
    # than all stopping at Config's unknown-key/type error. Retain a dataclass so
    # the runner's ordinary lane_config/replace path still runs unchanged.
    review_perspectives: list = field(default_factory=list)
    review_reviewers: int = 1


def perspective_case(git_history, tmp_path, monkeypatch, *, count=1,
                     risk_paths=("feature*.txt",), extra=(), **settings):
    case = risk_case(git_history, tmp_path, monkeypatch,
                     entries=[entry(list(risk_paths))], **settings)
    case.config = case.ctx.config = ReviewConfig(**{
        **asdict(case.config), "review_reviewers": count,
        "review_perspectives": [dict(item) for item in (*CHECKLIST, *extra)],
    })
    return case


def assert_checklist(prompt, items):
    positions = []
    for item in items:
        assert item["name"] in prompt, f"Reviewer checklist is missing {item['name']}"
        assert item["focus"] in prompt, f"Reviewer checklist is missing focus: {item['focus']}"
        positions.append(prompt.index(item["focus"]))
    assert positions == sorted(positions), "Checklist must retain effective perspective order"


def test_review_perspectives_defaults_have_exact_order_and_required_focus():
    module = import_module("foragent.review_perspectives")
    defaults = module.DEFAULT_PERSPECTIVES
    assert [item["name"] for item in defaults] == list(NAMES)
    for item, themes in zip(defaults, THEMES):
        assert isinstance(item["focus"], str) and item["focus"].strip()
        for theme in themes:
            assert theme in item["focus"].lower(), f"{item['name']} must cover {theme}"
    config = Config()
    assert config.review_reviewers == 1
    assert config.review_perspectives == []
    effective = module.perspectives(config)
    assert isinstance(effective, list) and effective == list(defaults)


def test_review_perspectives_project_appends_and_overrides_in_place(tmp_path):
    override = {"name": "goal-fidelity", "focus": "PROJECT-GOAL: verify the approved outcome"}
    path = tmp_path / ".foragent.yml"
    # Put the addition first in YAML: it must still follow all four defaults.
    path.write_text(yaml.safe_dump({
        "review_perspectives": [PROJECT, override], "review_reviewers": 3,
    }), encoding="utf-8")
    config = load_config(tmp_path)
    assert config.review_reviewers == 3
    module = import_module("foragent.review_perspectives")
    before = [dict(item) for item in module.DEFAULT_PERSPECTIVES]
    effective = module.perspectives(config)
    assert isinstance(effective, list)
    assert [item["name"] for item in effective] == [*NAMES, PROJECT["name"]]
    assert effective[0] == override
    assert effective[1:len(NAMES)] == before[1:]
    assert effective[-1] == PROJECT
    assert list(module.DEFAULT_PERSPECTIVES) == before, "Project overrides must not mutate defaults"
    assert module.perspectives(Config()) == before, "Project settings must not leak into another project"


def test_review_perspectives_config_accepts_valid_settings_and_rejects_bad_shapes(tmp_path):
    path = tmp_path / ".foragent.yml"
    valid = {"review_perspectives": [], "review_reviewers": 1}
    path.write_text(yaml.safe_dump(valid), encoding="utf-8")
    # Unknown-key rejection must never count as validation of the new settings.
    config = load_config(tmp_path)
    assert config.review_reviewers == 1 and config.review_perspectives == []
    invalid = [
        ("review_perspectives", {}),
        ("review_perspectives", "security"),
        ("review_perspectives", [{"name": "security"}]),
        ("review_perspectives", [{"focus": "Input provenance"}]),
        ("review_perspectives", [None]),
        ("review_reviewers", 0),
        ("review_reviewers", -1),
        ("review_reviewers", 1.5),
        ("review_reviewers", "2"),
        ("review_reviewers", True),
    ]
    for key, value in invalid:
        path.write_text(yaml.safe_dump({**valid, key: value}), encoding="utf-8")
        with pytest.raises(ConfigError, match=key):
            load_config(tmp_path)


def test_review_perspectives_single_risk_reviewer_receives_effective_checklist(
    git_history, tmp_path, monkeypatch,
):
    for reviewer, project in (("codex", True), ("codex", False), ("claude", True), ("claude", False)):
        root = tmp_path / f"{reviewer}-{'project' if project else 'defaults'}"
        history = copy_history(git_history, root)
        if project:
            case = perspective_case(history, root, monkeypatch, reviewer=reviewer, extra=(PROJECT,))
            expected = [*CHECKLIST, PROJECT]
        else:
            case = risk_case(history, root, monkeypatch, reviewer=reviewer, entries=[entry()])
            expected = import_module("foragent.review_perspectives").DEFAULT_PERSPECTIVES
        # runner_support calls the implementer `executor`; risk_case names it
        # `implementer`. Adapt that fixture name before reusing its recorder.
        case.executor = case.implementer
        calls = enable_reviewer(case, ["VERDICT: PASS"] * (len(expected) + 1)) if reviewer == "codex" else case.reviewer.calls
        assert run_issue(1, case.ctx) == "merged"
        assert len(calls) == len(expected) + 1, "N=1 queues all round-1 perspectives, then one merge"
        context = calls[-1]["prompt"] if reviewer == "codex" else case.review_contexts[-1]
        assert case.tracker.merged_prs
        assert_checklist(context, expected)



def test_review_perspectives_non_risk_prompt_omits_checklist_while_risk_prompt_has_it(
    git_history, tmp_path, monkeypatch,
):
    matched_root = tmp_path / "matched"
    matched_history = copy_history(git_history, matched_root)
    ordinary = perspective_case(git_history, tmp_path, monkeypatch, risk_paths=("docs/**",))
    assert run_issue(1, ordinary.ctx) == "merged"
    assert len(ordinary.reviewer.calls) == 1
    ordinary_prompt = ordinary.reviewer.calls[0]["prompt"]
    for item in CHECKLIST:
        assert item["focus"] not in ordinary_prompt, "Non-risk PR acquired a perspective checklist"

    # Pair the unchanged edge with a positive risk control: an implementation
    # that never emits any checklist must fail this test today and in future.
    matched = perspective_case(matched_history, matched_root, monkeypatch)
    assert run_issue(1, matched.ctx) == "merged"
    assert len(matched.reviewer.calls) == len(CHECKLIST) + 1, "Risk round covers every perspective, then merges"
    assert_checklist(matched.reviewer.calls[-1]["prompt"], CHECKLIST)
    for item in import_module("foragent.review_perspectives").DEFAULT_PERSPECTIVES:
        assert item["focus"] not in ordinary_prompt
