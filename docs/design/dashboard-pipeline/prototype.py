"""THROWAWAY PROTOTYPE (11-07): the 11-stage order pipeline under the bubble.

Switched on by FA_PIPELINE_PROTO=A|B|C. Reads local state files directly; not for merge.
"""

from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ORDER = 13

STATE = Path.home() / ".local/share/foragent/JinYoon-builds__foragent"


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _age(seconds):
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    return f"{m}분" if m < 60 else f"{m // 60}시간 {m % 60}분" if m % 60 else f"{m // 60}시간"


def facts(snapshot) -> list[dict[str, Any]]:
    now = time.time()
    lanes = snapshot.status.get("lanes", {})
    current = [x for lane in lanes.values() for x in lane.get("current", [])]
    nxt = [x for lane in lanes.values() for x in lane.get("next", [])]
    # Ready records: the latest 'ready' outcome per issue, from the local flow log.
    ready, first = {}, {}
    try:
        for line in (STATE / "flow-records.jsonl").read_text().splitlines():
            if '"kind": "ready"' in line:
                r = json.loads(line)
                ready[r["issue"]] = r
                if (r.get("data") or {}).get("outcome") == "started":
                    first.pop(r["issue"], None)
                else:
                    first.setdefault(r["issue"], r)
    except Exception:
        pass
    pubs = [d for d in (_load(f) for f in glob.glob(str(STATE / "order-publications/*.json"))) if d]
    pub_files = {d["id"]: os.path.getmtime(STATE / "order-publications" / f"{d['id']}.json") for d in pubs}
    pending = [d for d in pubs if d.get("status") in ("pending", "applying")]
    returned = [d for d in pubs if d.get("status") == "preparation"]
    preparing = len(glob.glob(str(STATE / "prepare/*/running*"))) + len(
        [d for d in (_load(f) for f in glob.glob(str(STATE / "order-jobs/*.json"))) if d and d.get("status") in ("running", "preparing")])
    planning = 1 if any(_load(f) for f in glob.glob(str(STATE / "order-progress*.json"))) else 0

    def phase(name):
        return [x for x in current if x.get("phase") == name]

    labels = {}
    try:
        from foragent.state import StateStore
        for row in StateStore.for_repo("JinYoon-builds/foragent").local_issues():
            labels[int(row["number"])] = set(row.get("labels") or [])
    except Exception:
        pass
    breakdown = {"바로 시작": 0, "파일 겹침": 0, "승인 없음": 0, "멈춰 둠": 0, "막힘": 0, "후보에서 빠짐": 0}
    for x in nxt:
        lab = labels.get(int(x["number"]), set())
        reason = (ready.get(x["number"], {}).get("data") or {}).get("reason")
        if x.get("kind") == "parked":
            breakdown["멈춰 둠"] += 1
        elif x.get("state") == "blocked" or "blocked" in lab:
            breakdown["막힘"] += 1
        elif "approved" not in lab:
            breakdown["승인 없음"] += 1
        elif reason == "file-overlap":
            breakdown["파일 겹침"] += 1
        elif "ready" in lab and reason is None:
            breakdown["바로 시작"] += 1
        else:
            breakdown["후보에서 빠짐"] += 1
    startable, held = [], {}
    for x in nxt:
        if x.get("kind") == "parked":
            held["멈춰 둠"] = held.get("멈춰 둠", 0) + 1
            continue
        if x.get("state") == "blocked":
            held["막힘"] = held.get("막힘", 0) + 1
            continue
        reason = (ready.get(x["number"], {}).get("data") or {}).get("reason")
        label = {"file-overlap": "파일 겹침", "outbox-wait": "GitHub 쓰기 대기", "held-no-retry": "재시도 대기",
                 None: None}.get(reason, reason)
        if label:
            held[label] = held.get(label, 0) + 1
        else:
            startable.append(x)
    queued_since = [datetime.fromisoformat(first[x["number"]]["at"]).timestamp()
                    for x in nxt if x["number"] in first and x.get("kind") != "parked"]
    merge_wait = [x for x in current if x.get("state") == "waiting-merge"]
    # Batch / owner checks: the newest open merge-batch record still without a CI result.
    files = sorted(glob.glob(str(STATE / "merge-batch/*.json")), key=os.path.getmtime)
    batch = 1 if files and now - os.path.getmtime(files[-1]) < 60 * 60 else 0
    stages = [
        {"n": "①", "name": "기획", "count": planning, "age": None},
        {"n": "②", "name": "시험 준비", "count": preparing + len(returned), "age": None,
         "note": f"되돌아옴 {len(returned)}" if returned else None},
        {"n": "③", "name": "발행", "count": len(pending),
         "age": (now - min(pub_files[d["id"]] for d in pending)) if pending else None},
        {"n": "④", "name": "레인 대기", "count": len(startable) + sum(held.values()),
         "age": (now - min(queued_since)) if queued_since else None, "startable": breakdown["바로 시작"],
         "held": {k: v for k, v in breakdown.items() if k != "바로 시작" and v}, "breakdown": breakdown},
        {"n": "⑤", "name": "구현", "count": len(phase("implementing")) + len(phase("baseline")), "age": None},
        {"n": "⑥", "name": "확인", "count": len(phase("checking")), "age": None},
        {"n": "⑦", "name": "리뷰", "count": len(phase("reviewing")), "age": None},
        {"n": "⑧", "name": "PR CI", "count": len(phase("ci")), "age": None},
        {"n": "⑨", "name": "머지 대기", "count": len(merge_wait), "age": None},
        {"n": "⑩", "name": "묶음", "count": batch, "age": None},
        {"n": "⑪", "name": "오늘 머지", "count": getattr(snapshot, "today", 0) or 0, "age": None},
    ]
    stuck = max((s for s in stages[:10] if s["count"] and s["age"] is not None), key=lambda s: s["age"], default=None)
    for s in stages:
        s["stuck"] = s is stuck and s["age"] is not None and s["age"] >= 30 * 60
    # Idle lanes beside startable work is a stall even when its age is unrecorded.
    working = sum(s["count"] for s in stages[4:8])
    if not any(s["stuck"] for s in stages) and stages[3].get("startable", 0) > working:
        stages[3]["stuck"] = True
    return stages


def _cell(s):
    if os.environ.get("FA_PIPELINE_PROTO") == "COLLAPSED":  # chips like acorns
        selected = os.environ.get("FA_PIPE_OPEN") == str("①②③④⑤⑥⑦⑧⑨⑩⑪".index(s["n"]) + 1)
        if s["name"] == "오늘 머지":
            chip = ("chip", 16, 120 if selected else 71)
        elif s["stuck"]:
            chip = ("chip", 231, 196 if selected else 167)
        elif s["count"]:
            chip = ("chip", 16, 228 if selected else 179)
        else:
            chip = ("chip", 16 if selected else 247, 252 if selected else 238)
        text = f' {s["n"]} {s["name"]} {s["count"]} '
        if selected:
            return [("▐", ("chip", 231, None)), (text, chip), ("▌", ("chip", 231, None))]
        return [(text, chip)]
    style = "question" if s["stuck"] else ("bold" if s["count"] else "next")
    text = f'{s["n"]} {s["name"]} {s["count"]}'
    spans = [(text, style)]
    if s["stuck"]:
        held = s.get("held") or {}
        cause = max(held.items(), key=lambda kv: kv[1])[0] if held else ""
        age = _age(s["age"]) if s["age"] and s["age"] >= 1800 else ""
        spans.append((" ⚠ " + " ".join(x for x in (cause, age) if x), "question"))
    return spans


def render(snapshot, config, repo_root: Path, **context: Any):
    variant = os.environ.get("FA_PIPELINE_PROTO")
    if not variant or getattr(snapshot, "status", None) is None:
        return []
    width = context.get("width", 100)
    stages = facts(snapshot)
    if variant == "COLLAPSED":
        os.environ.setdefault("FA_PIPE_ARROW", "▶")
    if variant in ("A", "COLLAPSED"):
        arrow = os.environ.get("FA_PIPE_ARROW", "→")
        lines, line, used = [], [], 0
        for i, s in enumerate(stages):
            spans = ([(f" {arrow} ", "dim")] if i else [(" ", "road")]) + _cell(s)
            size = sum(_w(t) for t, _ in spans)
            if used + size > width - 1 and line:
                lines.append(line); line, used = [(" ", "road")], 1
                spans = spans[1:] if i else spans
                size = sum(_w(t) for t, _ in spans)
            line += spans; used += size
        lines.append(line)
        lw = stages[3]
        if variant == "COLLAPSED":
            gap = os.environ.get("FA_PIPE_GAP")
            if gap == "blank":
                lines.insert(0, [])
            elif gap == "line":
                lines.insert(0, [("━" * width, "separator")])
            if os.environ.get("FA_PIPE_OPEN") == "WINDOW":
                rows = [(k, v) for k, v in lw["breakdown"].items()]
                title = f' ④ 레인 대기 {lw["count"]} '
                inner = max(_w(title) + 2, 28)
                box = [[("   ╭─" + title + "─" * (inner - _w(title) - 1) + "╮", "bold")]]
                for k, v in rows:
                    pad = inner - _w(k) - len(str(v)) - 2
                    style = "green" if k == "바로 시작" and v else ("bold" if v else "next")
                    box.append([("   │ ", "bold"), (k, style), (" " * pad, ""), (str(v), style), (" │", "bold")])
                box.append([("   │ ", "bold"), ("숫자를 누르면 이슈 목록", "next"),
                            (" " * (inner - _w("숫자를 누르면 이슈 목록") - 1), ""), ("│", "bold")])
                box.append([("   ╰" + "─" * inner + "╯", "bold")])
                lines += box
            return lines
        if lw["count"]:
            labels = {"released": "풀려남"}
            parts = [(f' ④ 레인 대기 {lw["count"]} = ', "dim"), (f'바로 시작 {lw.get("startable", 0)}', "green")]
            for k, v in sorted((lw.get("held") or {}).items(), key=lambda kv: -kv[1]):
                parts.append((f" · {labels.get(k, k)} {v}", "dim"))
            lines.append(parts)
        return lines
    if variant == "B":
        top = max(1, max(s["count"] for s in stages[:10]))
        bar_room = max(6, width - 40)

        def bar(s):
            k = 0 if not s["count"] else max(1, round(s["count"] / top * bar_room / 3))
            return [("█" * k if k else "·", "question" if s["stuck"] else ("working" if s["count"] else "next"))]

        def group(title, items):
            out = [(f" {title:<6}", "dim")]
            for s in items:
                out += [("  ", "road"), (f'{s["n"]}{s["name"]} ', "bold" if s["count"] else "next")] + bar(s)
                out += [(f' {s["count"]}', "bold" if s["count"] else "next")]
                if s["stuck"]:
                    out.append((f' ⚠{_age(s["age"])}' if s["age"] and s["age"] >= 1800 else " ⚠", "question"))
            return out
        s = stages
        lane_wait = s[3]
        held = lane_wait.get("held") or {}
        held_line = [(" " * 9 + "④ 바로 시작 가능 ", "dim"), (str(lane_wait.get("startable", 0)), "green")] + [
            (f" · {k} {v}", "dim") for k, v in sorted(held.items(), key=lambda kv: -kv[1])]
        return [group("들어오는", s[0:3]), group("레인", s[3:5]), held_line, group("", s[5:8]),
                group("나가는", s[8:11])]
    if variant == "C":
        working = sum(s["count"] for s in stages[4:8])
        merged = stages[10]["count"]
        stuck = next((s for s in stages if s["stuck"]), None)
        first = [(" 흐름 ", "dim"), ("정상" if not stuck else "막힘", "green" if not stuck else "question"),
                 (f" · 오늘 머지 {merged} · 지금 작업 {working}개", "dim")]
        if not stuck:
            return [first]
        cause = ""
        if stuck["name"] == "레인 대기":
            held = stuck.get("held") or {}
            if held:
                k, v = max(held.items(), key=lambda kv: kv[1])
                cause = f" (가장 많은 이유: {k} {v}개, 바로 시작 가능 {stuck.get('startable', 0)}개)"
        since = f"{_age(stuck['age'])}째 " if stuck["age"] is not None and stuck["age"] >= 30 * 60 else ""
        warn = [(f" ⚠ {stuck['n']} {stuck['name']}에서 막혔어요: {stuck['count']}개가 {since}기다려요{cause}", "question")]
        return [first, warn]
    return []


def _w(text):
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
