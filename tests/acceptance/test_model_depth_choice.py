"""Acceptance test for model-depth-choice. Locked by Foragent; do not edit."""

from dataclasses import replace
import json

import pytest
import yaml

from foragent.config import Config, ConfigError, load_config
from foragent.runner import run_issue
from test_executor_choice import recorded_usage
from test_local_truth import record_gh
from test_orch_review_handoff import make_case
from test_skills_autosync import isolated_home


@pytest.fixture(autouse=True)
def model_depth_offline(monkeypatch, isolated_home):
    monkeypatch.setenv("FORAGENT_ORCH_SPAWN", "off")
    calls = record_gh(monkeypatch)
    yield
    assert calls == [], "Model/depth choice must use recorded quota without gh calls"


def model_depth_tiers():
    # Distinct values expose a model/depth taken from the wrong executor or tier.
    return {
        "s": {"codex_model": "codex-small", "codex_effort": "medium",
              "claude_model": "haiku", "claude_effort": "low"},
        "m": {"codex_model": "codex-medium", "codex_effort": "high",
              "claude_model": "sonnet", "claude_effort": "medium"},
        "l": {"codex_model": "codex-large", "codex_effort": "xhigh",
              "claude_model": "opus", "claude_effort": "high"},
    }


# #186 has no ample-threshold setting. Use tiers_headroom_ample, default 0.5.
# All resets are one hour away, so the remaining fraction and #186's hourly
# headroom coincide. Claude's usable quota is measured against its 80% cap.
QUOTA_WINDOWS = {
    "ample": {"codex": (("7d", 25, 1),), "claude": (("five_hour", 5, 1),)},
    "at-threshold": {"codex": (("7d", 50, 1),), "claude": (("five_hour", 30, 1),)},
    "tight": {"codex": (("7d", 51, 1),), "claude": (("five_hour", 31, 1),)},
    "bottleneck": {
        "codex": (("7d", 25, 1), ("5h", 51, 1)),
        "claude": (("seven_day", 5, 1), ("five_hour", 31, 1)),
    },
}


def model_depth_case(git_history, tmp_path, monkeypatch, *, executor, size,
                     quota="tight", **config_values):
    other = "claude" if executor == "codex" else "codex"
    case = make_case(
        git_history, tmp_path, monkeypatch,
        **{"executor": other, "reviewer": other, "skill_libraries": [],
           "test_command": "true", "tiers": model_depth_tiers(),
           "lanes": {"orch": {"paths": ["**"], "executor": executor}},
           **config_values},
    )
    # A fixed lane is a public #186 choice. Deliberately choose the opposite of
    # the configured default, and give the reviewer ample quota: reading either
    # of those instead of the chosen executor would upgrade tight cases to l.
    case.tracker.add_labels(1, ["lane:orch", f"size:{size}"])
    case.store.update_run(1, lane="orch")
    windows = {"codex": (("7d", 0, 1),), "claude": (("five_hour", 0, 1),)}
    windows[executor] = QUOTA_WINDOWS[quota][executor]
    case.usage = recorded_usage(case.clock[0], **windows)
    # #186 injects this public read boundary while the D60 reader is unmerged.
    monkeypatch.setattr(case.store, "usage", lambda: case.usage, raising=False)
    case.ctx.executor_factory = {executor: case.implementer, other: case.reviewer}.__getitem__
    case.reviewer.steps = [json.dumps({"result": "(none)"}) if other == "claude" else "VERDICT: PASS"]
    return case


def received_model_depth(case):
    return [(call.get("model"), call.get("effort")) for call in case.implementer.calls]


def expected_model_depth(case, executor, *tiers):
    return [(case.ctx.config.tiers[tier][f"{executor}_model"],
             case.ctx.config.tiers[tier][f"{executor}_effort"]) for tier in tiers]


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_config_defaults_expose_both_model_and_depth_fields(tmp_path, size):
    (tmp_path / ".foragent.yml").write_text("{}\n", encoding="utf-8")
    required = {"codex_model", "codex_effort", "claude_model", "claude_effort"}
    for config in (Config(), load_config(tmp_path, tmp_path / "absent-global.yml")):
        assert required <= config.tiers[size].keys(), f"tiers.{size} needs {sorted(required)}"


@pytest.mark.parametrize("empty_models", [False, True], ids=["named-models", "empty-models"])
def test_config_loads_model_depth_tiers_and_keeps_unknown_keys_strict(tmp_path, empty_models):
    tiers = model_depth_tiers()
    if empty_models:
        for tier in tiers.values():
            tier.update(codex_model="", claude_model="")
    path = tmp_path / ".foragent.yml"
    global_path = tmp_path / "absent-global.yml"
    path.write_text(yaml.safe_dump({"tiers": tiers}), encoding="utf-8")
    assert load_config(tmp_path, global_path).tiers == tiers

    path.write_text(yaml.safe_dump({"tiers": tiers, "tiers_headroom_ampl": 0.5}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path, global_path)
    tiers["s"]["claude_efort"] = "low"
    path.write_text(yaml.safe_dump({"tiers": tiers}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path, global_path)


def test_config_default_ample_headroom_is_half(tmp_path):
    (tmp_path / ".foragent.yml").write_text("{}\n", encoding="utf-8")
    for config in (Config(), load_config(tmp_path, tmp_path / "absent-global.yml")):
        assert getattr(config, "tiers_headroom_ample", None) == 0.5, (
            "tiers_headroom_ample must default to 0.5"
        )


@pytest.mark.parametrize("executor", ["codex", "claude"])
@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("quota", ["ample", "at-threshold", "tight", "bottleneck"])
def test_chosen_executor_headroom_selects_model_and_depth(
    git_history, tmp_path, monkeypatch, executor, size, quota,
):
    case = model_depth_case(git_history, tmp_path, monkeypatch,
                            executor=executor, size=size, quota=quota)
    tier = "l" if quota in {"ample", "at-threshold"} else size
    assert run_issue(1, case.ctx) == "merged"
    assert received_model_depth(case) == expected_model_depth(case, executor, tier), (
        f"{executor}, size:{size}, {quota}: must receive tier {tier}'s model AND depth"
    )


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_configured_headroom_threshold_controls_tier(git_history, tmp_path, monkeypatch, executor):
    case = model_depth_case(git_history, tmp_path, monkeypatch,
                            executor=executor, size="m", quota="ample")
    # 75% usable quota is ample by default, but tight against an explicit 80%.
    path = case.root / ".foragent.yml"
    path.write_text(yaml.safe_dump({"tiers_headroom_ample": 0.8}), encoding="utf-8")
    loaded = load_config(case.root, tmp_path / "absent-global.yml")
    assert loaded.tiers_headroom_ample == 0.8
    case.ctx.config = replace(case.ctx.config, tiers_headroom_ample=loaded.tiers_headroom_ample)
    assert run_issue(1, case.ctx) == "merged"
    assert received_model_depth(case) == expected_model_depth(case, executor, "m")


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_tight_small_issue_escalates_s_m_l_with_max_retries_three(
    git_history, tmp_path, monkeypatch, executor,
):
    case = model_depth_case(git_history, tmp_path, monkeypatch,
                            executor=executor, size="s", max_retries=3)
    # Missing feature.txt fails the real Done when check twice; no runner
    # failure, quota decision, or tier-selection logic is patched.
    case.implementer.steps = ["Incomplete", "Incomplete", case.implement]
    assert run_issue(1, case.ctx) == "merged"
    assert received_model_depth(case) == expected_model_depth(case, executor, "s", "m", "l")


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_last_allowed_attempt_uses_highest_model_and_depth(
    git_history, tmp_path, monkeypatch, executor,
):
    # Preserve max_retries' existing meaning: one retry permits two attempts.
    # Even when the budget cannot visit m, the final attempt must reach l.
    case = model_depth_case(git_history, tmp_path, monkeypatch,
                            executor=executor, size="s", max_retries=1)
    case.implementer.steps = ["Incomplete", case.implement]
    assert run_issue(1, case.ctx) == "merged"
    assert received_model_depth(case) == expected_model_depth(case, executor, "s", "l")


@pytest.mark.parametrize("executor", ["codex", "claude"])
def test_empty_model_uses_executor_default(git_history, tmp_path, monkeypatch, executor):
    tiers = model_depth_tiers()
    tiers["s"][f"{executor}_model"] = ""
    case = model_depth_case(git_history, tmp_path, monkeypatch,
                            executor=executor, size="s", tiers=tiers,
                            executor_model="global-model")
    assert run_issue(1, case.ctx) == "merged"
    assert len(case.implementer.calls) == 1
    # At the fake executor boundary, None (or an omitted kwarg) means no model
    # flag. An explicit empty tier means executor default, even with a global
    # model configured; forwarding an empty string would produce a model flag.
    assert case.implementer.calls[0].get("model") is None, (
        "An empty tier model must omit the model flag, including a global fallback"
    )
    assert case.implementer.calls[0].get("effort") == tiers["s"][f"{executor}_effort"]
