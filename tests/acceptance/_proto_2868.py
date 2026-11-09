"""Prototype of #2868 (r1293) for break-it checks only; load with -p _proto_2868 (PYTHONPATH tests/acceptance).

MODE picks the variant: "shared" is the intended behaviour; the variants under _proto_2868_*.py each break it.
"""
import re

import pytest

MODE = "shared"


def install(monkeypatch, mode):
    from foragent import order_done_when, order_prepare_locks as L, order_reviewed, order_waiting
    from foragent.issue import MARKER
    from foragent.order_files import acceptance_links, issue_body
    marker = re.compile(r"<!-- /?foragent:(?:(?!-->).)*-->")

    if mode != "unfolded":
        record = order_reviewed._record

        def folded(plan, repo):
            return {key: {**entry, "notes": [" ".join(str(note).split()) for note in entry.get("notes", [])]}
                    if isinstance(entry, dict) and "notes" in entry else entry
                    for key, entry in record(plan, repo).items()}
        monkeypatch.setattr(order_reviewed, "_record", folded)

    def generated(footer):
        footer = order_waiting.replace_section(order_done_when.without_record(footer), None)
        out, notes = [], False
        for line in footer.split("\n"):
            if line == "## Preparation notes":
                notes = True
                continue
            if notes and marker.fullmatch(line):
                notes = False
            if notes:
                if mode == "r1291" or line.startswith("- ") or not line:
                    continue
            if marker.fullmatch(line):
                continue
            out.append(line)
        return "\n".join(out)

    def _restates(item, body, repo, base, acceptance_dir):
        if not set(item) <= L._RESTATED or repo is None:
            return False
        waiting = order_waiting.section(body)
        if waiting is not None:  # The seed carries the gate's wait in its goal (order_done_when.seed).
            item = {**item, "goal": item["goal"].removesuffix(f"\n\n## Waiting\n{waiting}")}
        links = [line + "\n" for line in acceptance_links(item, repo, base, acceptance_dir)]
        authored = issue_body(item, repo, base, acceptance_dir).removeprefix(MARKER + "\n")
        authored = authored.removesuffix("".join(links))
        current = body.removeprefix(MARKER + "\n")
        if not current.startswith(authored):
            return False
        footer = current[len(authored):]
        for link in links:
            footer = footer.removeprefix(link)
        return mode == "any" or not generated(footer).strip()
    monkeypatch.setattr(L, "_restates", _restates)


@pytest.fixture(autouse=True)
def _proto(monkeypatch):
    install(monkeypatch, MODE)
