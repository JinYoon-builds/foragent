"""Full lane-view sketch (D57-4, D59, D60, D62, D64). Loops: prep -> publish -> running -> done."""
import random
import re
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from cast import COL, ORCH_GATHER, TREE  # noqa: E402
from anim import RESET, DIM, acorn_chip, cells, publish_scene, WAVE  # noqa: E402

BOLD = "\x1b[1m"
W = 100


def rgb(r, g, b):
    return "\x1b[38;2;%d;%d;%dm" % (r, g, b)


def vis(s):
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s)) + sum(1 for ch in re.sub(r"\x1b\[[0-9;]*m", "", s)
                                                         if "가" <= ch <= "힣" or ch in "🧭🌳🐿⏳⏰💤✋✓")


def pad(s, n):
    return s + " " * max(0, n - vis(s))


GNAW = [
    ["....O.O..", "WT..OOOO.", "TT.OOEOCC", "TT.OOOWAA", "TT.OWWOA.", ".TOOWWO..", "..OOOOO..", "..O...O.."],
    ["....O.O..", "WT..OOOO.", "TT.OOEOO.", "TT.OOOWCC", "TT.OWWOAA", ".TOOWWOA.", "..OOOOO..", "..O...O.."],
]
SIT = [["....O.O..", "WT..OOOO.", "TT.OOEOO.", "TT.OOOWWN", "TT.OWWO..", ".TOOWWO..", "..OOOOO..", "..O...O.."],
       ["....O.O..", "WT..OOOO.", "TT.OOKOO.", "TT.OOOWWN", "TT.OWWO..", ".TOOWWO..", "..OOOOO..", "..O...O.."]]
SLEEP = [[".........", ".......Z.", "......Z..", ".........", "WTT.OOOO.", "TTTOOKOOW", "TTTOOOOOO", ".TTOOOOO."],
         [".........", "........Z", ".........", "......Z..", "WTT.OOOO.", "TTTOOKOOW", "TTTOOOOOO", ".TTOOOOO."]]
WAVE8 = WAVE

LANES = [  # name, badge, pose, issues, done count, line
    ("orch", "✺", "gnaw", [93, 94, 95, 96, 97, 98], 3, "#96 사용량 읽기 · 구현 중 · 12분"),
    ("view", "✺", "wave", [99, 100, 101, 102, 103, 104], 1, f"{rgb(255,210,60)}{BOLD}#100 말투 기준 · 머지할까요? ← Jin{RESET}"),
    ("order", "✺", "sit", [105, 106, 107, 108], 1, "⏳ #106 · #96이 끝나길 기다려요"),
    ("skills", "✺", "gnaw", [109, 110, 111], 1, "#110 링크로 스킬 가져오기 · 리뷰 중"),
    ("board", "✺", "sleep", [112], 1, "모두 끝냈어요"),
]


def pose_frames(pose):
    return {"gnaw": GNAW, "sit": SIT, "sleep": SLEEP, "wave": WAVE8}[pose]


def header(text):
    return [f" {BOLD}foragent{RESET} · {text}", " " + DIM + "─" * (W - 2) + RESET]


def footer(mode="내 몫 남기기"):
    c = f" Claude {rgb(120,200,120)}41%{RESET} · 3시 리셋   Codex {rgb(230,170,70)}67%{RESET} · 일 리셋   메모리 여유 · 동시 4/5"
    return [" " + DIM + "─" * (W - 2) + RESET, pad(c, W - 22) + f"모드: {mode}  {DIM}[m]{RESET}"]


def orch_box(now, why, nxt, ask, tree=True):
    sq = cells(ORCH_GATHER)
    tr = cells(TREE) if tree else []
    text = ["", f"{BOLD}🧭 오케스트레이터{RESET}  {DIM}Orca 탭 \"orch: foragent\"{RESET}", "",
            f"지금  {now}", f"이유  {DIM}{why}{RESET}", f"다음  {nxt}", "", ask]
    out = []
    for i in range(max(len(sq), len(tr))):
        left = "".join(sq[i]) if i < len(sq) else " " * 18
        mid = text[i] if i < len(text) else ""
        right = "".join(tr[i]) if i < len(tr) else ""
        out.append("  " + pad(left, 18) + "  " + pad(mid, 58) + right)
    if tree:
        out.append(" " * 80 + f"{BOLD} GitHub{RESET}")
    return out


def lane_block(name, badge, pose, nums, done, line, f):
    icon = cells(pose_frames(pose)[f % len(pose_frames(pose))])
    parts = [acorn_chip(n, i < done) for i, n in enumerate(nums)]
    chip_rows = [" ".join(p[k] for p in parts) for k in range(3)]
    out = []
    for i in range(max(4, len(icon))):
        left = f"  {BOLD}{name:<7}{RESET}{badge} " if i == 1 else " " * 11
        sq = "".join(icon[i]) if i < len(icon) else ""
        right = chip_rows[i] if i < 3 else ("  " + line if i == 3 else "")
        out.append(left + pad(sq, 10) + right)
    return out


def draw(lines):
    sys.stdout.write("\x1b[H\x1b[J" + "\n".join(lines) + "\n")
    sys.stdout.flush()


def prep_scene():
    total = 8
    for n in range(total + 1):
        for f in range(2):
            pile = " ".join(acorn_chip(90 + i, True)[1] for i in range(n))
            caps = " ".join(acorn_chip(90 + i, True)[0] for i in range(n))
            tips = " ".join(acorn_chip(90 + i, True)[2] for i in range(n))
            steps = [("문서 읽고 작업 정리", "✓"), (f"완료 시험 쓰기 {n}/{total}", "✓" if n == total else "⛏"),
                     ("지금 실패하는지 확인", "·"), ("Jin 승인 받기 ← 여기서 Jin이 필요해요", "·"), ("발행", "·")]
            lines = header("주문 준비 중 · D56~D62")
            lines += orch_box("완료 시험을 쓰고 있어요", "이슈마다 끝났는지 기계가 확인할 기준이에요",
                              "다 쓰면 지금은 모두 실패하는지 돌려 볼게요", "", tree=False)
            lines += [""] + [f"    {s}  {t}" for t, s in steps] + [""]
            lines += ["    " + caps, "    " + pile, "    " + tips, ""]
            lines += footer()
            draw(lines)
            time.sleep(0.25)
    time.sleep(0.8)


def running_scene(seconds=9):
    for f in range(int(seconds / 0.3)):
        lines = header("주문 D56~D62 · 이슈 20개 · 7개 끝 · 3개 일하는 중 · " + f"{rgb(255,210,60)}Jin을 기다려요 1{RESET}")
        lines += orch_box("#96이 막혀서 원인을 보고 있어요", "시험 설명과 실제 동작이 달라요",
                          "설명을 고쳐서 다시 맡길게요", f"{rgb(255,210,60)}Jin이 볼 것  view #100 머지 결정{RESET}")
        lines += [""]
        for lane in LANES:
            lines += lane_block(*lane, f) + [""]
        lines += footer()
        draw(lines)
        time.sleep(0.3)


def done_scene():
    random.seed(3)
    cols = [(255, 90, 90), (255, 200, 60), (90, 200, 255), (160, 255, 120), (230, 130, 255)]
    bursts = [(12, 4, 0), (40, 3, 3), (70, 5, 6), (88, 3, 9), (26, 6, 11)]
    for frame in range(22):
        canvas = [[" "] * W for _ in range(10)]
        for bx, by, start in bursts:
            r = frame - start
            if 0 <= r <= 5:
                c = cols[(bx + by) % len(cols)]
                for dx, dy in [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]:
                    x, y = bx + dx * r * 2, by + dy * r
                    if 0 <= x < W and 0 <= y < 10:
                        canvas[y][x] = rgb(*c) + ("✦" if r < 4 else "·") + RESET
        lines = header("주문 D56~D62 · 이슈 20개 모두 끝")
        lines += ["".join(row) for row in canvas]
        lines += ["", f"          {BOLD}이번 주문을 모두 끝냈어요 ✓{RESET}  20개 머지", ""]
        lines += footer()
        draw(lines)
        time.sleep(0.12)
    lines = header("주문 D56~D62 · 이슈 20개 모두 끝")
    lines += orch_box("모든 레인이 쉬고 있어요", "이번 주문의 이슈 20개를 모두 머지했어요",
                      "새 주문을 기다려요", f"{rgb(120,200,120)}쿼터가 남아 있어요. /fa로 다음 일을 주면 바로 써요.{RESET}")
    lines += ["", f"  {BOLD}끝낸 일{RESET}"]
    for num, what, lane in [(93, "머지 뒤에 foragent를 다시 설치해요", "orch"), (99, "말투 기준을 한 장으로 정했어요", "view"),
                            (105, "인터뷰가 문서를 먼저 읽어요", "order"), (109, "스킬 목록이 없으면 저절로 내려받아요", "skills")]:
        lines.append(f"    {rgb(120,200,120)}✓{RESET} #{num} {pad(what, 40)} {DIM}{lane} · 머지{RESET}")
    lines.append(f"    {DIM}… 16개 더{RESET}")
    lines += [""]
    for lane in LANES:
        lines += lane_block(lane[0], lane[1], "sleep", lane[3], len(lane[3]), "모두 끝냈어요", 0)
    lines += footer()
    draw(lines)
    time.sleep(4)


if __name__ == "__main__":
    sys.stdout.write("\x1b[?25l")
    try:
        while True:
            prep_scene()
            publish_scene([90, 91, 92, 93, 94, 95])
            running_scene()
            done_scene()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h")
