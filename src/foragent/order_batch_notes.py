"""Applying queued planning notes to their files, once, before any write (D85, #1043)."""

from pathlib import Path, PurePosixPath

from foragent import order, order_references
from foragent.i18n import text
from foragent.order_files import check_write, write_atomic
from foragent.paths import repo_path

# decision and idea notes append to their history; a rule note edits one current-rules line.
NOTE_FILES = {"decision": "docs/decisions.md", "idea": "docs/ideas.md", "rule": "docs/rules.md"}
COMMITTED = "committed"


def _rule_lines(name: str, current: str, entry: dict, language: str) -> str:
    """Replace one current-rules line, or add the line when the note replaces none.

    Notes always apply to the file as it was before this batch wrote anything; an
    interrupted write is undone from the journal, so nothing is judged from current text.
    """
    lines = current.splitlines()
    if "replace" not in entry:
        return current + ("" if not current or current.endswith("\n") else "\n") + entry["text"] + "\n"
    if entry["replace"] not in lines:
        raise order.PlanError(text("batch_rule_line_missing", language, path=name, line=entry["replace"]))
    lines[lines.index(entry["replace"])] = entry["text"]
    return "\n".join(lines) + ("\n" if current.endswith("\n") else "")


def _note_files(root: Path, notes: list[dict], language: str) -> dict[str, str]:
    """Each note file with these notes applied in order; computed before any write."""
    contents: dict[str, str] = {}
    for entry in notes:
        name = NOTE_FILES[entry["kind"]]
        if name not in contents:
            # The destination is checked before it is read: a linked file or parent never
            # puts another file's bytes into the batch commit.
            check_write(root, name, PurePosixPath(name).parent.as_posix())
            path = repo_path(root, name)
            if entry["kind"] == "rule" and not path.is_file():
                # A project without current rules records the decision only.
                raise order.PlanError(text("batch_rules_missing", language, path=name))
            contents[name] = path.read_text(encoding="utf-8") if path.is_file() else ""
        if entry["kind"] == "rule":
            contents[name] = _rule_lines(name, contents[name], entry, language)
        else:
            current = contents[name]
            separator = "" if not current or current.endswith("\n\n") else "\n" if current.endswith("\n") else "\n\n"
            contents[name] = f"{current}{separator}{entry['text']}\n"
    return contents


def _write_notes(root: Path, contents: dict[str, str]) -> None:
    for name, content in contents.items():
        path = repo_path(root, name)
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            continue
        own_root = PurePosixPath(name).parent.as_posix()
        check_write(root, name, own_root)
        order_references.real_directory(root, own_root)
        check_write(root, name, own_root)
        write_atomic(root, name, content.encode("utf-8"))
