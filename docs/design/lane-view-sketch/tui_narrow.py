"""Side-pane lane view sketch: fits ~48 columns. Loops prep -> publish -> running -> done."""
import math
import re
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from cast import COL, ORCH_GATHER, TREE  # noqa: E402
from anim import RESET, DIM, acorn_chip, cells, stamp, blank, text_at  # noqa: E402
from tui import BOLD, GNAW, SIT, SLEEP, WAVE8, rgb, pad  # noqa: E402

import shutil
W = 48


def width():
    return max(40, shutil.get_terminal_size((48, 40)).columns - 1)
YEL = rgb(255, 210, 60)
GRN = rgb(120, 200, 120)


STATE = {"log": False}


def read_key(timeout):
    import select
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        seq = sys.stdin.read(2)
        if seq == "[M":  # X10 mouse report: button, x, y
            b = ord(sys.stdin.read(1)) - 32
            STATE["mx"], STATE["my"] = ord(sys.stdin.read(1)) - 33, ord(sys.stdin.read(1)) - 33
            if b == 64:
                return "wheel-up"
            if b == 65:
                return "wheel-down"
            return "click" if (b & 3) == 0 else None
        return None
    return ch


def enter_pressed(timeout):
    """Wait up to timeout seconds. True on Enter; l or a mouse click toggles the log."""
    end = time.time() + timeout
    while True:
        left = end - time.time()
        if left <= 0:
            return False
        k = read_key(left)
        if k in ("\n", "\r"):
            return True
        if k == "click":
            hit = acorn_at(STATE.get("mx", -1), STATE.get("my", -1))
            if STATE.get("detail") is not None:
                STATE["detail"] = None
            elif isinstance(hit, tuple):
                STATE.setdefault("open", set()).symmetric_difference_update({hit[1]})
            elif hit is not None:
                STATE["detail"] = hit
            else:
                STATE["log"] = not STATE["log"]
            return False
        if k in ("a", "A"):
            STATE["all_open"] = not STATE.get("all_open")
            return False
        if k in ("x", "\x7f"):
            STATE["detail"] = None
            return False
        if k in ("l", "L"):
            STATE["log"] = not STATE["log"]
            return False
        if k in ("wheel-up", "k"):
            STATE["scroll"] = max(0, STATE.get("scroll", 0) - 3)
            return False
        if k in ("wheel-down", "j"):
            STATE["scroll"] = STATE.get("scroll", 0) + 3
            return False


def hint(text="Enter 다음 · 도토리 클릭 이슈 · ▸ 펼치기 · a 모두 · l 기록"):
    return f" {DIM}{text}{RESET}"


def vis(t):
    plain = re.sub(r"\x1b\[[0-9;]*m", "", t)
    return len(plain) + sum(1 for ch in plain if "\uac00" <= ch <= "\ud7a3")


def fit(t, n):
    out = ""
    for ch in t:
        if vis(out + ch) > n - 1 and vis(t) > n:
            return out + "…"
        out += ch
    return out


HITS = []  # (line_index, col_start, col_end, issue)
ISSUES = {
    96: ("Read plan usage and wait exactly until reset", "구현 중 · 12분", "orch",
         "실행기 출력에서 사용량과 리셋 시각을 읽고, 한도에 닿으면 리셋까지 쉬었다가 다시 돌린다.",
         "uv run pytest tests/acceptance/test_usage_read.py"),
    100: ("One-page voice standard", "머지할까요? ← Jin", "view",
          "말투 기준 한 장을 쓰고 화면·알림 문구를 그 기준으로 고친다.",
          "uv run pytest tests/acceptance/test_voice.py"),
}


def wrap(text, n):
    out, cur = [], ""
    for word in text.split(" "):
        if cur and vis(cur + " " + word) > n:
            out.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    return out + ([cur] if cur else [])


def acorn_at(x, y):
    top = STATE.get("scroll", 0)
    line = top + y
    for li, c0, c1, num in HITS:
        if li == line and c0 <= x < c1:
            return num
    return None


def detail_box(num):
    title, state, lane_name, goal, done = ISSUES.get(num, (f"이슈 #{num}", "기다리는 중", "?", "(내용은 로컬 기록에서 읽어요)", "-"))
    inner = min(46, width() - 6)
    def row(t):
        return f"  {DIM}│{RESET} {pad(fit(t, inner - 2), inner - 2)} {DIM}│{RESET}"
    nut = "\x1b[48;2;%d;%d;%dm\x1b[38;2;255;240;200m\x1b[1m" % COL["A"]
    return [f"  {DIM}╭{'─' * inner}╮{RESET}",
            f"  {DIM}│{RESET} {nut}#{num}{RESET} {pad(fit(title, inner - 8), inner - 4 - len(str(num)))} {DIM}│{RESET}",
            row(f"{lane_name} 레인 · {state}"), row(""),
            row("목표"), *[row("  " + w) for w in wrap(goal, inner - 6)], row(""),
            row("끝났다는 기준"), row(f"  {done}"), row(""),
            row("GitHub에서 보기 ↗   [x] 또는 클릭으로 닫기"),
            f"  {DIM}╰{'─' * inner}╯{RESET}"]


def rule():
    return " " + DIM + "─" * (width() - 2) + RESET


def head(text):
    return [f" {BOLD}foragent{RESET} {DIM}·{RESET} {text}", rule()]


def foot(mode="내 몫 남기기"):
    return [rule(),
            f" Claude {GRN}41%{RESET}{DIM}·3시{RESET}  Codex {rgb(230,170,70)}67%{RESET}{DIM}·일{RESET}  동시 4/5",
            f" 메모리 여유  {DIM}모드:{RESET} {mode} {DIM}[m]{RESET}"]


def dots(done, total):
    return rgb(150, 95, 50) + "●" * done + RESET + rgb(90, 90, 90) + "●" * (total - done) + RESET


def lane(name, pose, nums, done, line1, line2, f):
    """Small squirrel on the left; status lines and numbered acorns (wrapped to the pane width) on the right."""
    frames = {"gnaw": GNAW, "sit": SIT, "sleep": SLEEP, "wave": WAVE8}[pose]
    icon = cells(frames[f % len(frames)])
    chips = [acorn_chip(n, i < done) for i, n in enumerate(nums)]
    room = width() - 18
    opened = STATE.get("all_open") or name in STATE.setdefault("open", set())
    per_row = max(1, room // 7)
    toggle = None
    if not opened and len(chips) > per_row:
        keep = max(1, per_row - 1)  # leave room for the toggle chip
        hidden = len(chips) - keep
        start = max(0, min(done - 1, len(chips) - keep))  # last finished, the current one, then what comes next
        chips = chips[start:start + keep]
        toggle = ("", f"{DIM}▸ {hidden}개 더{RESET}", "")
    elif opened and len(chips) > per_row:
        toggle = ("", f"{DIM}▾ 접기{RESET}", "")
    rows_of_chips, cur, used = [], [], 0
    for c in chips + ([toggle] if toggle else []):
        w = len(re.sub(r"\x1b\[[0-9;]*m", "", c[1])) + 1
        if cur and used + w > room:
            rows_of_chips.append(cur)
            cur, used = [], 0
        cur.append(c)
        used += w
    if cur:
        rows_of_chips.append(cur)
    right = [line1, line2]
    rel_hits = []
    for g, group in enumerate(rows_of_chips):
        col = 18
        for c in group:
            w = len(re.sub(r"\x1b\[[0-9;]*m", "", c[1]))
            label = re.sub(r"\x1b\[[0-9;]*m", "", c[1])
            num = ("lane", name) if label.startswith(("▸", "▾")) else int(label.strip("▐▌#"))
            for k in range(3):
                rel_hits.append((2 + g * 3 + k, col, col + w, num))
            col += w + 1
        for k in range(3):
            right.append(" ".join(c[k] for c in group))
    lane.last_hits = rel_hits
    out = []
    for i in range(max(len(icon), len(right))):
        left = f" {BOLD}{name:<6}{RESET}" if i == 1 else " " * 7
        sq = "".join(icon[i]) if i < len(icon) else ""
        out.append(left + pad(sq, 11) + (right[i] if i < len(right) else ""))
    return out


def progress_block(done=7, total=20, last_hour=3):
    """Plain words: how far, when it ends (clock time), and why; plus quota in words."""
    import datetime
    w = max(10, min(24, width() - 22))
    filled = round(w * done / total)
    bar = GRN + "█" * filled + RESET + DIM + "░" * (w - filled) + RESET
    left = total - done
    out = [f" {BOLD}진행{RESET}  {bar} {done}/{total}"]
    if last_hour:
        end = datetime.datetime.now() + datetime.timedelta(hours=left / last_hour)
        ampm = "새벽" if end.hour < 6 else ("오전" if end.hour < 12 else ("오후" if end.hour < 18 else "밤"))
        h = end.hour % 12 or 12
        out += [f"       이 속도면 {BOLD}{ampm} {h}시 {end.minute // 10 * 10}분쯤{RESET} 끝나요",
                f"       {DIM}(최근 1시간에 {last_hour}개 끝냈어요){RESET}"]
    else:
        out += [f"       {DIM}끝나는 시각은 아직 몰라요 (최근 1시간에 끝낸 게 없어요){RESET}"]
    out.append(f" {BOLD}쿼터{RESET}  Codex 67% · 일요일 리셋 전에 다 써요")
    return out


def draw(lines):
    rows = shutil.get_terminal_size((48, 40)).lines - 1
    if len(lines) <= rows:
        STATE["scroll"] = 0
        view = lines
    else:
        top = min(STATE.get("scroll", 0), len(lines) - rows)
        STATE["scroll"] = top
        view = lines[top:top + rows - 1] + [f" {DIM}휠 또는 j/k로 스크롤 · {top + 1}–{top + rows - 1}/{len(lines)}줄{RESET}"]
    sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(view))
    sys.stdout.flush()


def prep():
    total = 8
    for n in range(total + 1):
        sq = cells(ORCH_GATHER)
        lines = head("주문 준비 중")
        lines += ["  " + "".join(r) for r in sq]
        lines += ["", f" {BOLD}🧭 완료 시험을 쓰고 있어요{RESET}  {n}/{total}",
                  f" {DIM}다 쓰면 지금 실패하는지 돌려 볼게요{RESET}", ""]
        steps = [("✓", "문서 읽고 작업 정리"), ("✓" if n == total else "⛏", f"완료 시험 {n}/{total}"),
                 ("·", "실패하는지 확인"), ("·", f"Jin 승인 {YEL}← Jin 필요{RESET}"), ("·", "발행")]
        lines += [f"   {a} {b}" for a, b in steps] + [""]
        chips = [acorn_chip(90 + i, True) for i in range(n)]
        per = max(1, (width() - 4) // 7)
        for g in range(0, len(chips), per):
            for k in range(3):
                lines.append("  " + " ".join(r[k] for r in chips[g:g + per]))
        lines += foot() + [hint()]
        draw(lines)
        if enter_pressed(1.0):
            return
    while not enter_pressed(10):
        pass


def publish(issues):
    cw, ch = 46, 20
    ox, oy, tx, ty = 0, 4, 32, 6
    hollow = (tx + 5, ty + 6)
    start = (ox + 16, oy + 9)
    for idx, num in enumerate(issues):
        for s in range(15):
            t = s / 14
            x = start[0] + (hollow[0] - start[0]) * t
            y = start[1] + (hollow[1] - start[1]) * t - 6 * math.sin(math.pi * t)
            canvas = blank(cw, ch)
            stamp(canvas, ORCH_GATHER, ox, oy)
            stamp(canvas, TREE, tx, ty)
            rows = cells(canvas)
            cx, cy = int(round(x)) - 1, int(round(y)) // 2
            label = f"#{num}"
            text_at(rows, cx, cy - 1, "▄" * len(label), "\x1b[38;2;%d;%d;%dm" % COL["C"])
            text_at(rows, cx, cy, label, "\x1b[48;2;%d;%d;%dm\x1b[38;2;255;240;200m\x1b[1m" % COL["A"])
            text_at(rows, tx + 3, ch // 2 - 1, "GitHub", BOLD)
            lines = head(f"발행 중 {idx}/{len(issues)}") + ["  " + "".join(r) for r in rows]
            lines += ["", f" {BOLD}🧭 도토리를 GitHub 나무로 보내요{RESET}"] + foot() + [hint()]
            draw(lines)
            if enter_pressed(0.07):
                return


LANES = [
    ("orch", "gnaw", list(range(93, 99)) + list(range(120, 131)), 3, f"{BOLD}#96{RESET} 사용량 읽기", f"{DIM}구현 중 · 12분{RESET}"),
    ("view", "wave", [99, 100, 101, 102, 103, 104], 1, f"{YEL}{BOLD}#100 머지할까요? ← Jin{RESET}", f"{DIM}말투 기준{RESET}"),
    ("order", "sit", [105, 106, 107, 108], 1, f"{BOLD}#106{RESET} 인터뷰 다시 쓰기", f"{DIM}⏳ #96이 끝나길 기다려요{RESET}"),
    ("skills", "gnaw", [109, 110, 111], 1, f"{BOLD}#110{RESET} 링크로 스킬 가져오기", f"{DIM}리뷰 중{RESET}"),
    ("board", "sleep", [112], 1, "모두 끝냈어요", ""),
]


def running(seconds=None):
    f = -1
    while True:
        f += 1
        lines = head(f"D56~D62 · 7/20 끝 · {YEL}Jin 1{RESET}")
        side_by_side = width() >= 56
        inner = min(34, width() - (25 if side_by_side else 6))
        t1, t2 = fit("#109를 다시 맡겼어요.", inner - 2), fit("기본 인터뷰를 native로 고치래요", inner - 2)
        box = [f"{DIM}╭{'─' * inner}╮{RESET}",
               f"{DIM}│{RESET} {pad(t1, inner - 2)} {DIM}│{RESET}",
               f"{DIM}│{RESET} {DIM}{pad(t2, inner - 2)}{RESET} {DIM}│{RESET}"]
        tag = " [l] 기록 "
        bottom = f"{DIM}╰{'─' * (inner - vis(tag))}{tag}╯{RESET}"
        who = [f"🧭 {BOLD}오케스트레이터{RESET} {DIM}21:23{RESET}", f"{YEL}Jin이 볼 것 · view #100 머지{RESET}"]
        sq = cells(ORCH_GATHER)
        if side_by_side:
            # tail on the left of the middle row, pointing at the squirrel
            bubble = ["", "  " + box[0], "  " + box[1], f"{DIM}<─┤{RESET}" + box[2][len(DIM) + 1 + len(RESET):],
                      "  " + bottom, ""] + ["  " + w for w in who]
            for i in range(max(len(sq), len(bubble))):
                left = "".join(sq[i]) if i < len(sq) else ""
                lines.append("  " + pad(left, 19) + (bubble[i] if i < len(bubble) else ""))
        else:
            # stacked: tail drops down from the bubble toward the squirrel below
            tail_bottom = f"{DIM}╰──┬{'─' * (inner - 3 - vis(tag))}{tag}╯{RESET}"
            lines += ["  " + w for w in who] + ["", "  " + box[0], "  " + box[1], "  " + box[2],
                      "  " + tail_bottom, f"  {DIM}   │{RESET}", f"  {DIM}   ▼{RESET}"]
            lines += ["  " + "".join(r) for r in sq]
        if STATE["log"]:
            lines += [rule(), f" {BOLD}오케스트레이터 기록{RESET}  {DIM}[l] 닫기{RESET}",
                      f"  {DIM}21:23{RESET} #109 다시 맡겼어요 — 기본 인터뷰를 native로",
                      f"        {DIM}바꾸래요(D58). 시험 두 개가 그걸로 통과해요.{RESET}",
                      f"  {DIM}19:42{RESET} #91 다시 맡겼어요 — init 뒤에는 레인 화면을",
                      f"        {DIM}항상 열기로 Jin이 정했어요.{RESET}",
                      f"  {DIM}19:10{RESET} #93 머지했어요",
                      f"  {DIM}18:55{RESET} #92 리뷰 지적 1개를 고쳐서 다시 맡겼어요"]
        lines += [rule()] + progress_block() + [rule()]
        if STATE.get("detail") is not None:
            lines += detail_box(STATE["detail"]) + [rule()]
        HITS.clear()
        for ln in LANES:
            block = lane(*ln, f)
            HITS.extend((len(lines) + r, c0, c1, n) for r, c0, c1, n in lane.last_hits)
            lines += block + [""]
        lines += foot() + [hint()]
        draw(lines)
        if enter_pressed(0.45):
            return


def done():
    import random
    random.seed(5)
    cols = [(255, 90, 90), (255, 200, 60), (90, 200, 255), (160, 255, 120), (230, 130, 255)]
    bursts = [(8, 3, 0), (26, 2, 3), (40, 4, 6), (16, 5, 9)]
    sq = cells(ORCH_GATHER)
    for frame in range(20):
        canvas = [[" "] * W for _ in range(8)]
        for bx, by, st in bursts:
            r = frame - st
            if 0 <= r <= 4:
                c = cols[(bx + by) % len(cols)]
                for dx, dy in [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]:
                    x, y = bx + dx * r * 2, by + dy * r
                    if 0 <= x < W and 0 <= y < 8:
                        canvas[y][x] = rgb(*c) + ("✦" if r < 3 else "·") + RESET
        lines = head("D56~D62 · 20/20 끝")
        lines += ["".join(r) for r in canvas] + ["  " + "".join(r) for r in sq]
        lines += ["", f" {BOLD}이번 주문을 모두 끝냈어요 ✓{RESET}  20개 머지"] + foot()
        draw(lines)
        time.sleep(0.2)
    lines = head("D56~D62 · 20/20 끝") + ["  " + "".join(r) for r in sq]
    lines += ["", f" {BOLD}이번 주문을 모두 끝냈어요 ✓{RESET}", ""]
    for num, what in [(93, "머지 뒤 다시 설치"), (99, "말투 기준 한 장"), (105, "문서 먼저 읽는 인터뷰"), (109, "스킬 저절로 내려받기")]:
        lines.append(f"  {GRN}✓{RESET} #{num} {what}")
    lines += [f"  {DIM}… 16개 더{RESET}", "", f" {GRN}쿼터가 남아 있어요.{RESET}", f" {GRN}/fa로 다음 일을 주면 바로 써요.{RESET}"]
    lines += foot() + [hint("Enter ▶ 처음부터 다시")]
    draw(lines)
    while not enter_pressed(10):
        pass


if __name__ == "__main__":
    import termios
    import tty
    saved = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h")
    try:
        while True:
            prep()
            publish([90, 91, 92, 93, 94])
            running()
            done()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?1000l\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
