"""D64 cast: big orchestrator squirrel (2 poses) + small lane squirrels (4 poses)."""
import time

COL = {"K": (70, 35, 20), "O": (222, 122, 48), "R": (176, 84, 30), "W": (250, 245, 235),
       "E": (25, 15, 10), "N": (120, 50, 25), "A": (150, 95, 50), "C": (95, 60, 30),
       "P": (240, 120, 90), "T": (222, 122, 48), "G": (90, 160, 80), "B": (110, 70, 40),
       "H": (40, 25, 15), "Z": (150, 180, 255), "X": (255, 210, 60)}


def render(grid):
    w = max(map(len, grid))
    g = [r.ljust(w, ".") for r in grid]
    if len(g) % 2:
        g.append("." * w)
    out = []
    for t, b in zip(g[0::2], g[1::2]):
        s = ""
        for x, y in zip(t, b):
            if x == "." and y == ".":
                s += " "
            elif x != "." and y != ".":
                s += "\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀\x1b[0m" % (*COL[x], *COL[y])
            elif x != ".":
                s += "\x1b[38;2;%d;%d;%dm▀\x1b[0m" % COL[x]
            else:
                s += "\x1b[38;2;%d;%d;%dm▄\x1b[0m" % COL[y]
        out.append(s)
    return out


def side(*blocks, gap=4):
    h = max(len(b) for b in blocks)
    widths = [max((len(l.encode()) and visible(l)) for l in b) for b in blocks]
    rows = []
    for i in range(h):
        row = ""
        for b, w in zip(blocks, widths):
            line = b[i] if i < len(b) else ""
            row += line + " " * (w - visible(line) + gap)
        rows.append(row)
    return rows


def visible(s):
    import re
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


# Big orchestrator: pose 1 gathering (hugs an acorn)
ORCH_GATHER = [
    "......KK...KK.....",
    ".....KOOK.KOWK....",
    ".....KOOOKKOOOK...",
    "..KKKKOOOOOOOOOK..",
    ".KWWWKOOOOOOOOOOK.",
    "KWWWWKOOEOOOOEOOK.",
    "KWWWOKOPWWWNWWWPOK",
    "KOWOOKOOWWWWWWWOOK",
    "KOOOOKKOOWCCCWOOK.",
    "KROOOOKOOCAAACOOK.",
    ".KROOOKOOKAAAKOOK.",
    ".KRROOOKOOKKKOOK..",
    "..KKRRROOOOOOOK...",
    "....KKKKKOK.KOK...",
    "........KK...KK...",
]
# Big orchestrator: pose 2 putting an acorn into the tree (arm reaching right)
ORCH_PUT = [
    "......KK...KK.......",
    ".....KOOK.KOWK......",
    ".....KOOOKKOOOK.....",
    "..KKKKOOOOOOOOOK....",
    ".KWWWKOOOOOOOOOOK...",
    "KWWWWKOOEOOOOEOOK...",
    "KWWWOKOPWWWNWWWPOK..",
    "KOWOOKOOWWWWWWWOOKKK",
    "KOOOOKKOOWWWWWOOOOCC",
    "KROOOOKOOWWWWWOOKAAA",
    ".KROOOKOOOWWWOOK.AA.",
    ".KRROOOKOOOOOOOK....",
    "..KKRRROOOOOOOK.....",
    "....KKKKKOK.KOK.....",
    "........KK...KK.....",
]
TREE = [
    "...GGGGGG...",
    ".GGGGGGGGGG.",
    "GGGGGGGGGGGG",
    ".GGGGGGGGGG.",
    "....BBBB....",
    "...BHHHHB...",
    "...BHCAHB...",
    "...BHAAHB...",
    "...BHHHHB...",
    "....BBBB....",
    "...BBBBBB...",
]

# Small lane squirrels (9 x 8 px -> 4 rows)
SMALL = {
    "갉기 (일하는 중)": [
        "....O.O..",
        "WT..OOOO.",
        "TT.OOEOCC",
        "TT.OOOWAA",
        "TT.OWWOA.",
        ".TOOWWO..",
        "..OOOOO..",
        "..O...O..",
    ],
    "앉아 있기 (기다림)": [
        "....O.O..",
        "WT..OOOO.",
        "TT.OOEOO.",
        "TT.OOOWWN",
        "TT.OWWO..",
        ".TOOWWO..",
        "..OOOOO..",
        "..O...O..",
    ],
    "손 흔들기 (사람 필요)": [
        "....O.OXO",
        "WT..OOOOO",
        "TT.OOEOO.",
        "TT.OOOWWN",
        "TT.OWWO..",
        ".TOOWWO..",
        "..OOOOO..",
        "..O...O..",
    ],
    "잠 (끝)": [
        ".........",
        ".......Z.",
        "......Z..",
        ".........",
        "WTT.OOOO.",
        "TTTOOKOOW",
        "TTTOOOOOO",
        ".TTOOOOO.",
    ],
}
SMALL_WAVE2 = [
    "....O.O..",
    "WT..OOOOX",
    "TT.OOEOOO",
    "TT.OOOWWN",
    "TT.OWWO..",
    ".TOOWWO..",
    "..OOOOO..",
    "..O...O..",
]


def acorn(done):
    return ("\x1b[38;2;95;60;30m▄\x1b[38;2;150;95;50m█\x1b[0m" if done
            else "\x1b[38;2;90;90;90m▄█\x1b[0m")


def lane_row(name, pose, done, total, label, badge):
    icon = render(SMALL[pose])
    acorns = " ".join(acorn(i < done) for i in range(total))
    rows = []
    for i, line in enumerate(icon):
        if i == 1:
            rows.append(f"  {name:<6}{badge} {line}  {acorns}")
        elif i == 2:
            rows.append(f"  {'':<6}  {line}  \x1b[2m{label}\x1b[0m")
        else:
            rows.append(f"  {'':<6}  {line}")
    return rows


def main():
    print("\x1b[2J\x1b[H")
    print("  ① 오케스트레이터 (크게) — 줍기 / 넣기 + GitHub 나무\n")
    for line in side(render(ORCH_GATHER), render(ORCH_PUT), render(TREE)):
        print("  " + line)
    print("        줍기 (주문 준비)        넣기 (발행)                GitHub\n")
    print("  ② 레인 다람쥐 (작게) — 4가지 포즈\n")
    names = list(SMALL)
    for line in side(*(render(SMALL[n]) for n in names), gap=8):
        print("    " + line)
    print("    " + "".join(f"{n:<17}" for n in names) + "\n")
    print("  ③ 레인 화면에 넣으면\n")
    for row in (lane_row("orch", "갉기 (일하는 중)", 3, 6, "자동 머지 뒤 재설치 · 구현 중", "✺")
                + lane_row("view", "손 흔들기 (사람 필요)", 2, 6, "말투 기준 · 머지할까요? ← Jin", "✺")
                + lane_row("skills", "앉아 있기 (기다림)", 1, 3, "링크로 스킬 가져오기 · ⏰ 3시 리셋", "✺")
                + lane_row("board", "잠 (끝)", 1, 1, "모두 끝냈어요", "✺")):
        print(row)
    print()
    input("  Enter를 누르면 손 흔들기가 움직여요 ")
    for i in range(10):
        frame = SMALL["손 흔들기 (사람 필요)"] if i % 2 == 0 else SMALL_WAVE2
        print("\x1b[2A\x1b[J", end="")
        lines = render(frame)
        print("\x1b[2J\x1b[H\n  손 흔들기 (사람 필요) — 움직임\n")
        for line in lines:
            print("      " + line)
        time.sleep(0.35)
    input("\n  Enter를 누르면 닫혀요 ")


if __name__ == "__main__":
    main()
