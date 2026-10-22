"""Interview-phase sketch for the side pane: ledger -> big picture -> filling the 5-cell table -> lane map."""
import sys, time
sys.path.insert(0, __file__.rsplit("/", 1)[0])
import tui_narrow as T
from tui_narrow import BOLD, DIM, RESET, YEL, GRN, head, foot, rule, hint, draw, enter_pressed, cells, pad, vis, fit
from cast import ORCH_GATHER

SMALL_ORCH = ["....O.O..", "WT..OOOO.", "TT.OOEOO.", "TT.OOOWWN", "TT.OWWO..", ".TOOWWO..", "..OOOOO..", "..O...O.."]


def say(text1, text2=""):
    """Big orchestrator squirrel with a speech bubble (side by side when wide, stacked when narrow)."""
    sq = cells(ORCH_GATHER)
    side = T.width() >= 56
    inner = min(34, T.width() - (25 if side else 6))
    t1, t2 = fit(text1, inner - 2), fit(text2, inner - 2)
    top = f"{DIM}╭{'─' * inner}╮{RESET}"
    l1 = f"{DIM}│{RESET} {pad(t1, inner - 2)} {DIM}│{RESET}"
    l2 = f"{DIM}│{RESET} {DIM}{pad(t2, inner - 2)}{RESET} {DIM}│{RESET}"
    if side:
        bubble = ["", "", "  " + top, "  " + l1, f"{DIM}<─┤{RESET}" + l2[len(DIM) + 1 + len(RESET):],
                  f"  {DIM}╰{'─' * inner}╯{RESET}"]
        out = []
        for i in range(len(sq)):
            out.append("  " + pad("".join(sq[i]), 19) + (bubble[i] if i < len(bubble) else ""))
        return out
    return (["  " + top, "  " + l1, "  " + l2, f"  {DIM}╰──┬{'─' * (inner - 3)}╯{RESET}",
             f"  {DIM}   │{RESET}", f"  {DIM}   ▼{RESET}"] + ["  " + "".join(r) for r in sq])


def ledger():
    items = [("D18", "작업 안의 반복은 순정 /goal"), ("D28", "문서에 있는 건 묻지 않기"), ("D29", "인터뷰 엔진은 바꿔 끼우기"),
             ("D56", "스킬은 링크로 들이기"), ("D58", "장부 · 지금 말이 문서를 이김"), ("D60", "한도까지 밀어붙이기"),
             ("D61", "말투 한 장"), ("D62", "다 끝남 화면")]
    for n in range(len(items) + 1):
        lines = head(f"주문 인터뷰 · {YEL}1 장부 확인{RESET}") + [""]
        lines += say(f"문서를 읽고 있어요 {n}/{len(items)}", "읽은 것은 다시 묻지 않을게요")
        lines += ["", f" {BOLD}결정 장부{RESET}  {DIM}문서에서 읽은 것{RESET}"]
        for k, (d, t) in enumerate(items[:n]):
            lines.append(f"   {GRN}✓{RESET} {DIM}{d}{RESET} {t}")
        if n == len(items):
            lines += ["", f" {YEL}→ 왼쪽에서 \"이대로 맞나요?\"에 답해 주세요{RESET}"]
        lines += [""] + foot() + [hint()]
        draw(lines)
        if enter_pressed(0.6 if n < len(items) else 999):
            return


CHUNKS = ["스킬 가져오기", "인터뷰 장부", "스킬 고르기", "/fa 입구", "한도까지 돌리기", "말투", "다 끝남 화면"]


def big_picture():
    for n in range(len(CHUNKS) + 1):
        lines = head(f"주문 인터뷰 · {YEL}2 큰 그림{RESET}") + [""]
        lines += say("이번 주문을 덩어리로 나눴어요", "빼거나 합칠 게 있나요?")
        lines += ["", f" {BOLD}큰 그림{RESET}  {DIM}{n}개 덩어리{RESET}"]
        for c in CHUNKS[:n]:
            lines.append(f"   ▪ {c}")
        if n == len(CHUNKS):
            lines += ["", f" {YEL}→ 왼쪽에서 큰 그림을 확인해 주세요{RESET}"]
        lines += [""] + foot() + [hint()]
        draw(lines)
        if enter_pressed(0.5 if n < len(CHUNKS) else 999):
            return


def table():
    cols = ["목표", "안함", "완료", "레인", "스킬"]
    lanes = ["skills", "order", "orch", "order", "orch", "view", "view"]
    # fill order: (chunk, col) pairs, as answers or the agent's own reading arrive
    order = [(r, c) for c in range(5) for r in range(len(CHUNKS))]
    asked = {(1, 1): "인터뷰에서 '안 할 것'은?", (4, 2): "한도 기능은 어떻게 확인하죠?", (6, 4): "말투 스킬이 없어요. 찾아볼까요?"}
    filled = set()
    for step in range(len(order) + 1):
        if step:
            filled.add(order[step - 1])
        empty = len(CHUNKS) * 5 - len(filled)
        nxt = order[step] if step < len(order) else None
        q = asked.get(nxt, "") if nxt else ""
        lines = head(f"주문 인터뷰 · {YEL}3 빈칸 채우기{RESET} · 남은 빈칸 {empty}") + [""]
        if q:
            lines += say("Jin에게 물어볼게요", q)
        else:
            lines += say("문서와 코드로 채우고 있어요", "모르는 것만 물어볼게요")
        lines += ["", f" {DIM}{pad('덩어리', 16)}{'  '.join(cols)}{RESET}"]
        for r, ch in enumerate(CHUNKS):
            row = f"   {pad(fit(ch, 14), 14)}"
            for c in range(5):
                if (r, c) in filled:
                    mark = f"{GRN}{lanes[r][:4]:<4}{RESET}" if c == 3 else f"{GRN} ✓  {RESET}"
                elif nxt == (r, c):
                    mark = f"{YEL} ?  {RESET}"
                else:
                    mark = f"{DIM} ·  {RESET}"
                row += " " + mark
            lines.append(row)
        if empty == 0:
            lines += ["", f" {GRN}빈칸이 없어요. 인터뷰를 끝낼게요 ✓{RESET}"]
        lines += [""] + foot() + [hint()]
        draw(lines)
        if enter_pressed(1.6 if q else 0.25) and step:
            return
    while not enter_pressed(999):
        pass


def lane_map():
    groups = {"orch": 6, "view": 6, "order": 4, "skills": 3, "board": 1}
    lines = head(f"주문 인터뷰 · {YEL}4 레인 지도{RESET}") + [""]
    lines += say("이렇게 나눠서 돌릴게요", "완료 시험을 쓰기 전에 확인해 주세요")
    lines += ["", f" {BOLD}레인 지도{RESET}  {DIM}20개 · 처음에 5개가 동시에 출발{RESET}", ""]
    nut = "\x1b[38;2;95;95;95m▗▄▖\x1b[0m"
    for name, n in groups.items():
        lines.append(f"   {BOLD}{name:<7}{RESET}" + " ".join(["\x1b[38;2;95;95;95m●\x1b[0m"] * n) + f"  {DIM}{n}개{RESET}")
    lines += ["", f" {DIM}도토리에는 아직 번호가 없어요.{RESET}", f" {DIM}GitHub 나무에 들어갈 때 번호가 생겨요.{RESET}",
              "", f" {YEL}→ 왼쪽에서 레인 지도를 확인해 주세요{RESET}", ""] + foot() + [hint("Enter ▶ 처음부터")]
    draw(lines)
    while not enter_pressed(999):
        pass


if __name__ == "__main__":
    import termios, tty
    saved = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    sys.stdout.write("\x1b[?25l")
    try:
        while True:
            ledger(); big_picture(); table(); lane_map()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h")
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
