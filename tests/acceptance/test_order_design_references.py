"""Acceptance test for order-design-references. Locked by Foragent; do not edit."""

import base64
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from test_order import PublishingGit, plan, publishing
from test_publish_out_of_scope import publish_cli


HANDOFF = "docs/design/lane-view/HANDOFF.md"
DRAFT = "docs/design/lane-view/approved draft.html"
IMAGE = "docs/design/lane-view/approved.png"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
LIMIT = 1024 * 1024


class BytesPublishingGit(PublishingGit):
    """The existing offline publisher fake, with byte-preserving blob storage."""

    @staticmethod
    def _blob(data):
        return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()

    def uncommitted_paths(self, paths):
        return [name for name in paths if self.saved.get(name) != (self.root / name).read_bytes()]

    def commit_paths(self, paths, title):
        contents = {name: (self.root / name).read_bytes() for name in paths}
        self.events.append(("commit", title, contents))
        self.saved.update(contents)


@pytest.fixture
def design_order(publishing, plan):
    root, config, tracker, _, events = publishing
    plan["reference_files"] = {
        HANDOFF: "# Approved lane view\nKeep Jin's chosen copy: 진행 중\n",
        DRAFT: "<main>Approved layout and copy</main>\n",
        IMAGE: {"path": IMAGE, "base64": base64.b64encode(PNG).decode()},
    }
    return SimpleNamespace(root=root, config=config, tracker=tracker,
                           git=BytesPublishingGit(root, events), events=events, plan=plan)


def publish(case, **kwargs):
    from foragent.order import publish

    return publish(case.plan, case.root, case.config, case.tracker,
                   repo="owner/repo", git=case.git, **kwargs)


def cli_publish(case, monkeypatch, capsys):
    main = publish_cli((case.root, case.config, case.tracker, case.git, case.events), monkeypatch)
    path = case.root / "PLAN.json"
    path.write_text(json.dumps(case.plan), encoding="utf-8")
    capsys.readouterr()
    code = main(["order", "publish", str(path), "--repo-root", str(case.root), "--dry-run"])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_order_references_publish_text_and_decoded_image_in_the_spec_commit(design_order):
    case = design_order
    report = publish(case)
    commits = [event for event in case.events if event[0] == "commit"]
    assert len(commits) == 1
    expected = {f"docs/specs/{case.plan['id']}.md": case.plan["spec_markdown"].encode(),
                **{name: text.encode() for item in case.plan["issues"]
                   for name, text in item["acceptance_files"].items()},
                HANDOFF: case.plan["reference_files"][HANDOFF].encode(),
                DRAFT: case.plan["reference_files"][DRAFT].encode(), IMAGE: PNG}
    assert commits[0][2] == expected, "Spec, locked tests and reference bytes must share a commit"
    assert set(report.paths) == set(expected)
    assert all((case.root / name).read_bytes() == data for name, data in expected.items())
    assert not list(case.root.rglob("*.b64"))
    before = len(case.events)
    publish(case)
    assert not any(event[0] == "commit" for event in case.events[before:]), "An unchanged retry is idempotent"


def test_order_references_cli_dry_run_lists_every_reference_without_writes(design_order, monkeypatch, capsys):
    code, output = cli_publish(design_order, monkeypatch, capsys)
    assert code == 0, output
    files_line = next(line for line in output.splitlines() if line.startswith("Files:"))
    for name in (HANDOFF, DRAFT, IMAGE, f"docs/specs/{design_order.plan['id']}.md"):
        assert name in files_line
    assert design_order.events == [] and design_order.tracker.issues == {}
    assert not (design_order.root / "docs").exists()


@pytest.mark.parametrize("name", ["README.md", "docs/specs/escape.md", "/docs/design/escape.md",
                                 "docs/design/../escape.md", "docs/design", "docs/design-other/a.md",
                                 "docs/design//a.md", "docs/design/./a.md", "docs/design/a\\b.md",
                                 "docs/design/line\nbreak.md", "docs/design/line\rbreak.md"])
def test_order_references_cli_rejects_paths_outside_normalized_design_namespace(
    design_order, monkeypatch, capsys, name,
):
    design_order.plan["reference_files"] = {name: "approved draft"}
    code, output = cli_publish(design_order, monkeypatch, capsys)
    assert code != 0
    # The CLI renders errors on one line, including invalid multiline paths.
    displayed_name = name.replace("\n", " ").replace("\r", " ")
    assert "docs/design/" in output and displayed_name in output, output
    assert design_order.events == [] and design_order.tracker.issues == {}
    assert not (design_order.root / "docs").exists()


def test_order_references_cli_rejects_symlink_escape(design_order, monkeypatch, capsys):
    case = design_order
    outside = case.root / "not-design"
    outside.mkdir()
    (case.root / "docs").mkdir()
    (case.root / "docs/design").symlink_to(outside, target_is_directory=True)
    case.plan["reference_files"] = {HANDOFF: "must stay in docs/design"}
    code, output = cli_publish(case, monkeypatch, capsys)
    assert code != 0 and "docs/design/" in output, output
    assert not list(outside.iterdir()) and case.events == []


@pytest.mark.parametrize("binary", [False, True])
def test_order_references_cli_rejects_file_over_one_mib_before_writes(
    design_order, monkeypatch, capsys, binary,
):
    name = IMAGE if binary else HANDOFF
    # Multibyte text must be limited by encoded bytes, not character count.
    data = PNG + b"x" * (LIMIT + 1 - len(PNG)) if binary else "가" * (LIMIT // 3 + 1)
    value = {"path": name, "base64": base64.b64encode(data).decode()} if binary else data
    design_order.plan["reference_files"] = {name: value}
    code, output = cli_publish(design_order, monkeypatch, capsys)
    assert code != 0
    assert name in output and re.search(r"(?:1048576|1\s*MiB|1\s*MB)", output), output
    assert design_order.events == [] and design_order.tracker.issues == {}
    assert not (design_order.root / "docs").exists()


@pytest.mark.parametrize("binary", [False, True])
def test_order_references_accepts_exactly_one_mib(design_order, binary):
    name = IMAGE if binary else HANDOFF
    data = PNG + b"x" * (LIMIT - len(PNG)) if binary else "a" * LIMIT
    design_order.plan["reference_files"] = {
        name: {"path": name, "base64": base64.b64encode(data).decode()} if binary else data}
    assert name in publish(design_order, dry_run=True).paths


@pytest.mark.parametrize("extension,data", [("png", PNG), ("jpg", b"\xff\xd8\xff\xd9"),
                                            ("gif", b"GIF89a\x00\xff")])
def test_order_references_supports_each_image_extension(design_order, extension, data):
    name = f"docs/design/topic/chosen.{extension}"
    design_order.plan["reference_files"] = {name: {"path": name, "base64": base64.b64encode(data).decode()}}
    publish(design_order)
    assert design_order.git.saved[name] == data


@pytest.mark.parametrize("value", [
    {"path": IMAGE, "base64": "@@not-base64@@"},
    {"path": HANDOFF, "base64": base64.b64encode(PNG).decode()},
    {"path": IMAGE, "base64": 123},
    {"base64": base64.b64encode(PNG).decode()},
])
def test_order_references_rejects_invalid_image_object_before_writes(design_order, value):
    from foragent.order import PlanError

    # First prove support, so main's unknown-field error cannot satisfy this check.
    publish(design_order, dry_run=True)
    design_order.plan["reference_files"] = {IMAGE: value}
    with pytest.raises(PlanError, match="reference"):
        publish(design_order)
    assert design_order.events == []


def test_order_references_rejects_base64_for_non_image_path(design_order):
    from foragent.order import PlanError

    publish(design_order, dry_run=True)
    design_order.plan["reference_files"] = {HANDOFF: {"path": HANDOFF, "base64": "YQ=="}}
    with pytest.raises(PlanError, match=r"png.*jpg.*gif"):
        publish(design_order)
    assert design_order.events == []


def test_order_references_issue_bodies_use_explicit_selection_or_all_by_default(design_order):
    case = design_order
    case.plan["issues"][0]["references"] = [HANDOFF, IMAGE]
    publish(case)
    first, second = (case.tracker.get_issue(row.number).body for row in case.tracker.list_issues()
                     if row.title in [item["title"] for item in case.plan["issues"]])
    for body, names in ((first, [HANDOFF, IMAGE]), (second, [HANDOFF, DRAFT, IMAGE])):
        assert "## References\n" in body
        section = body.split("## References\n", 1)[1].split("\n## ", 1)[0]
        for name in names:
            assert name in section
        if DRAFT in names:
            assert f"https://github.com/owner/repo/blob/main/{DRAFT.replace(' ', '%20')}" in section
        else:
            assert DRAFT not in section


def test_order_references_rejects_issue_link_missing_from_reference_files(design_order):
    from foragent.order import PlanError

    publish(design_order, dry_run=True)
    design_order.plan["issues"][0]["references"] = ["docs/design/topic/missing.md"]
    with pytest.raises(PlanError, match=r"references.*missing"):
        publish(design_order)
    assert design_order.events == []


def test_order_references_changed_bytes_revoke_prior_issue_approval(design_order):
    case = design_order
    publish(case)
    children = [row.number for row in case.tracker.list_issues() if row.number != 1]
    for number in children:
        case.tracker.add_labels(number, ["approved", "ready"])
    case.plan["reference_files"][IMAGE]["base64"] = base64.b64encode(PNG + b"updated").decode()
    publish(case)
    assert all(not {"approved", "ready"} & case.tracker.get_issue(n).labels for n in children)


def test_order_references_edits_preserve_binary_revision_and_reference_links(design_order):
    case = design_order
    case.tracker.add_issue(9, "Existing visual issue", "old", ["approved", "ready"])
    case.plan["edits"] = [{"number": 9, "goal": "Use the approved design", "out_of_scope": "Other screens",
                           "done_when": ["true"], "references": [IMAGE]}]
    publish(case)
    assert IMAGE in case.tracker.get_issue(9).body
    case.tracker.add_labels(9, ["approved", "ready"])
    publish(case)
    assert {"approved", "ready"} <= case.tracker.get_issue(9).labels
    case.plan["reference_files"][IMAGE]["base64"] = base64.b64encode(PNG + b"updated").decode()
    publish(case)
    assert not {"approved", "ready"} & case.tracker.get_issue(9).labels


def test_order_references_preread_uses_selection_and_allows_explicit_empty_list(design_order):
    from foragent import order

    case = design_order
    # Validate the same plan the real entry point consumes before building input.
    publish(case, dry_run=True)
    item = case.plan["issues"][0]
    item["references"] = [HANDOFF]
    prompt = order.preread_target(case.plan, item, case.config, case.root)[1]
    assert case.plan["reference_files"][HANDOFF] in prompt
    assert IMAGE not in prompt and DRAFT not in prompt
    item["references"] = []
    assert "## References" not in order.preread_target(case.plan, item, case.config, case.root)[1]
    publish(case)
    first = next(row for row in case.tracker.list_issues() if row.title == item["title"])
    assert "## References" not in first.body


def test_order_references_preread_receives_fresh_text_and_viewable_image_files(design_order, monkeypatch):
    from foragent import order

    case = design_order
    old = case.root / HANDOFF
    old.parent.mkdir(parents=True)
    old.write_text("Stale unapproved handoff")
    calls = []

    class Reader:
        def run(self, **kwargs):
            calls.append(kwargs)
            prompt = kwargs["prompt"]
            assert case.plan["spec_markdown"] in prompt
            assert case.plan["reference_files"][HANDOFF] in prompt
            assert case.plan["reference_files"][DRAFT] in prompt
            assert IMAGE in prompt
            # Absolute image path on its own line is directly usable by the image viewer.
            images = [Path(line.strip()) for line in prompt.splitlines()
                      if line.strip().startswith(str(case.root)) and line.strip().endswith(".png")]
            assert images and all(path.read_bytes() == PNG for path in images)
            assert all(path.is_relative_to(case.root / ".foragent") for path in images)
            assert kwargs["read_only"] is True and kwargs["worktree"] == case.root
            return SimpleNamespace(exit_code=0, timed_out=False, last_message="NONE")

    monkeypatch.setattr(order, "executor_for", lambda name: Reader())
    assert all(not row["unknowns"] for row in order.preread(case.plan, case.root, case.config))
    assert len(calls) == len(case.plan["issues"])
    assert old.read_text() == "Stale unapproved handoff"
    assert not (case.root / IMAGE).exists() and case.events == []
    first_digest = order.preread_target(case.plan, case.plan["issues"][0], case.config, case.root)[2]
    case.plan["reference_files"][HANDOFF] += "Updated approved copy\n"
    assert order.preread_target(case.plan, case.plan["issues"][0], case.config, case.root)[2] != first_digest


def test_order_references_publish_help_describes_transport_and_limits(monkeypatch, capsys):
    from foragent.cli import main

    with pytest.raises(SystemExit) as result:
        main(["order", "publish", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for text in ("reference_files", '"path"', '"base64"', '"references"', "docs/design/", "png", "jpg", "gif"):
        assert text in help_text
    assert re.search(r"(?:1048576|1\s*MiB|1\s*MB)", help_text)

