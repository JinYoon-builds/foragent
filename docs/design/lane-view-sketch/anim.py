"""D64 preview 2: acorns fly to the GitHub tree, numbered acorns, bigger wave."""
import math
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from cast import COL, ORCH_GATHER, TREE  # noqa: E402

DIM, RESET = "\x1b[2m", "\x1b[0m"


def fg(c):
    return "\x1b[38;2;%d;%d;%dm" % COL[c]


def cells(canvas):
    """Pixel canvas (list of str, '.' empty) -> rows of cell strings (half-blocks)."""
    canvas = ["".join(r) if isinstance(r, list) else r for r in canvas]
    w = max(map(len, canvas))
    g = [r.ljust(w, ".") for r in canvas]
    if len(g) % 2:
        g.append("." * w)
    rows = []
    for t, b in zip(g[0::2], g[1::2]):
        row = []
        for x, y in zip(t, b):
            if x == "." and y == ".":
                row.append(" ")
            elif x != "." and y != ".":
                row.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀\x1b[0m" % (*COL[x], *COL[y]))
            elif x != ".":
                row.append(fg(x) + "▀" + RESET)
            else:
                row.append(fg(y) + "▄" + RESET)
        rows.append(row)
    return rows


def blank(w, h):
    return [["."] * w for _ in range(h)]


def stamp(canvas, sprite, x0, y0):
    for y, line in enumerate(sprite):
        for x, ch in enumerate(line):
            if ch != "." and 0 <= y0 + y < len(canvas) and 0 <= x0 + x < len(canvas[0]):
                canvas[y0 + y][x0 + x] = ch


ACORN = ["CC", "AA", "A."]  # 2 x 3 px: cap + nut


def text_at(rows, col, row, text, style=""):
    if 0 <= row < len(rows):
        for i, ch in enumerate(text):
            if 0 <= col + i < len(rows[row]):
                rows[row][col + i] = style + ch + RESET


def show(rows, header=""):
    out = ["\x1b[H\x1b[J", header, ""]
    out += ["  " + "".join(r) for r in rows]
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def publish_scene(issues):
    W, H = 64, 22
    orch_x, orch_y = 1, 6
    tree_x, tree_y = 50, 8
    hollow = (tree_x + 5, tree_y + 6)
    start = (orch_x + 17, orch_y + 9)  # acorn at the orchestrator's hands
    landed = []
    for idx, num in enumerate(issues):
        steps = 16
        for s in range(steps + 1):
            t = s / steps
            x = start[0] + (hollow[0] - start[0]) * t
            y = start[1] + (hollow[1] - start[1]) * t - 9 * math.sin(math.pi * t)
            canvas = blank(W, H)
            stamp(canvas, ORCH_GATHER, orch_x, orch_y)
            stamp(canvas, TREE, tree_x, tree_y)
            pass  # acorn drawn as a numbered chip below
            rows = cells(canvas)
            cx, cy = int(round(x)) - 1, int(round(y)) // 2
            text_at(rows, cx, cy - 1, "▄" * len(f"#{num}"), fg("C"))
            text_at(rows, cx, cy, f"#{num}", "\x1b[48;2;%d;%d;%dm\x1b[38;2;255;240;200m\x1b[1m" % COL["A"])
            text_at(rows, tree_x + 1, (tree_y + 12) // 2 + 1, "GitHub", "\x1b[1m")
            text_at(rows, tree_x - 2, 1, f"이슈 {len(landed)}/{len(issues)}", DIM)
            show(rows, f"  🧭 발행 중 · 도토리를 GitHub 나무로 보내고 있어요 {len(landed)}/{len(issues)}")
            time.sleep(0.03)
        landed.append(num)
    canvas = blank(W, H)
    stamp(canvas, ORCH_GATHER, orch_x, orch_y)
    stamp(canvas, TREE, tree_x, tree_y)
    rows = cells(canvas)
    text_at(rows, tree_x + 1, (tree_y + 12) // 2 + 1, "GitHub", "\x1b[1m")
    show(rows, f"  🧭 발행했어요 ✓ 이슈 {len(issues)}개가 GitHub에 올라갔어요")
    time.sleep(1.2)


# Small lane squirrel wave: 3 frames with big arm swing and a hop
WAVE = [
    [  # arm straight up, high
        "........X",
        "....O.O.O",
        "WT..OOOOO",
        "TT.OOEOO.",
        "TT.OOOWWN",
        "TT.OWWO..",
        ".TOOWWO..",
        "..OOOOO..",
        "..O...O..",
    ],
    [  # hop: whole body up 1px, arm out to the side
        "....O.O..",
        "WT..OOOO.",
        "TT.OOEOOOX",
        "TT.OOOWWN",
        "TT.OWWO..",
        ".TOOWWO..",
        "..OOOOO..",
        "..O...O..",
        ".........",
    ],
    [  # arm up, tilted the other way
        ".......X.",
        "....O.OO.",
        "WT..OOOO.",
        "TT.OOEOO.",
        "TT.OOOWWN",
        "TT.OWWO..",
        ".TOOWWO..",
        "..OOOOO..",
        "..O...O..",
    ],
]


def acorn_chip(num, done):
    """A 3-row acorn with the issue number on the nut: cap, nut (number), tip."""
    nut_rgb = COL["A"] if done else (95, 95, 95)
    cap_rgb = COL["C"] if done else (65, 65, 65)
    txt_rgb = (255, 240, 200) if done else (175, 175, 175)
    label = f"#{num}"
    n = len(label)
    cap = "\x1b[38;2;%d;%d;%dm" % cap_rgb + "▗" + "▄" * n + "▖" + RESET
    body = ("\x1b[38;2;%d;%d;%dm▐" % nut_rgb
            + "\x1b[48;2;%d;%d;%dm\x1b[38;2;%d;%d;%dm\x1b[1m" % (*nut_rgb, *txt_rgb) + label + RESET
            + "\x1b[38;2;%d;%d;%dm▌" % nut_rgb + RESET)
    pad = (n + 2 - 1) // 2
    tip = " " * pad + "\x1b[38;2;%d;%d;%dm▀" % nut_rgb + RESET + " " * (n + 2 - pad - 1)
    return cap, body, tip


def wave_scene():
    lanes = [
        ("orch", [93, 94, 95, 96, 97, 98], 3, "자동 머지 뒤 재설치 · 구현 중"),
        ("view", [99, 100, 101, 102, 103, 104], 2, "말투 기준 · 머지할까요? ← Jin"),
        ("skills", [105, 106, 107], 1, "링크로 스킬 가져오기 · ⏳ 순서 대기"),
    ]
    for f in range(18):
        frame = WAVE[f % 3]
        out = ["\x1b[H\x1b[J", "  🐿 레인 화면 · view 레인이 손을 흔들어요 (사람 필요)", ""]
        for name, nums, done, label in lanes:
            icon = cells(frame) if name == "view" else cells(WAVE[1] if False else [
                "....O.O..", "WT..OOOO.", "TT.OOEOCC", "TT.OOOWAA",
                "TT.OWWOA.", ".TOOWWO..", "..OOOOO..", "..O...O..", "........."])
            parts = [acorn_chip(n, i < done) for i, n in enumerate(nums)]
            chip_rows = ["  ".join(p[k] for p in parts) for k in range(3)]
            for i, row in enumerate(icon):
                left = f"  {name:<7}" if i == 1 else " " * 9
                right = chip_rows[i] if i < 3 else (DIM + label + RESET if i == 3 else "")
                out.append(left + "".join(row) + "   " + right)
            out.append("")
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        time.sleep(0.28)


if __name__ == "__main__":
    sys.stdout.write("\x1b[?25l")
    try:
        publish_scene([90, 91, 92, 93, 94, 95])
        wave_scene()
        input("\n  Enter를 누르면 처음부터 다시 봐요 (Ctrl-C로 닫기) ")
        while True:
            publish_scene([90, 91, 92, 93, 94, 95])
            wave_scene()
            input("\n  Enter를 누르면 다시 봐요 ")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        sys.stdout.write("\x1b[?25h")
