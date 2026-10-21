"""Throwaway mock of the Foragent lane conveyor TUI (fake data, no Foragent calls)."""

import random
import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

PICK = ["  ⛏ ", " ⛏  ", "⛏✦  ", " ⛏  "]
SLEEP = ["  z ", "  zZ", " zZz", "  zZ"]
HAND = [" ✋ ", " ✋!", " ✋ ", " ✋!"]


class Lane:
    def __init__(self, name, agent, color, issues):
        self.name, self.agent, self.color = name, agent, color
        self.queue = issues
        self.current = None
        self.progress = 0
        self.started = time.time()
        self.done = []
        self.waiting_human = False

    def step(self):
        if self.waiting_human:
            return
        if self.current is None and self.queue:
            self.current = self.queue.pop(0)
            self.progress = 0
            self.started = time.time()
        if self.current is not None:
            self.progress += random.randint(4, 11)
            if self.current == 7 and self.progress > 60:
                self.waiting_human = True
                return
            if self.progress >= 100:
                self.done.append(self.current)
                self.current = None

    def phase(self):
        if self.waiting_human:
            return "사람을 기다림 (결정 필요)"
        if self.current is None:
            return "할 일 없음"
        p = self.progress
        return "구현 중" if p < 55 else "확인 명령 실행" if p < 75 else "리뷰 받는 중" if p < 92 else "머지 대기"


ROAD_WIDTH = 74
ASPHALT = "on grey15"


def edge():
    return Text("  " + "━" * ROAD_WIDTH, style="bold white")


def divider(tick):
    # A dashed centre line that scrolls toward the agents, like road markings in motion.
    pattern = "▬▬▬   "
    dashes = (pattern * (ROAD_WIDTH // len(pattern) + 1))[:ROAD_WIDTH]
    return Text("  " + dashes, style="yellow " + ASPHALT)


def pad_road(line):
    # Fill the rest of the lane with asphalt so each lane reads as one solid strip.
    gap = ROAD_WIDTH + 2 - line.cell_len
    if gap > 0:
        line.append(" " * gap, style=ASPHALT)
    return line


def render(lanes, tick):
    rows = [edge()]
    for i, lane in enumerate(lanes):
        line = Text("  ", style=ASPHALT)
        line.append(f" {lane.name:<6}", style="bold white " + ASPHALT)
        line.append("▸ ", style="dim " + ASPHALT)
        queue = "".join(f"[#{n}] " for n in reversed(lane.queue))
        line.append(f"{queue:>24}", style="white " + ASPHALT)
        if lane.current is not None:
            line.append(f"[#{lane.current}]", style=f"bold {lane.color} " + ASPHALT)
        else:
            line.append("    ", style=ASPHALT)
        if lane.waiting_human:
            tool = HAND[tick % 4]
        elif lane.current is None:
            tool = SLEEP[tick % 4]
        else:
            tool = PICK[tick % 4]
        line.append(tool, style="yellow " + ASPHALT)
        badge = "✳" if lane.agent == "Claude" else "✺"
        badge_style = "bold orange1" if lane.agent == "Claude" else "bold white"
        line.append(" ", style=ASPHALT)
        line.append(f"▐ {badge} {lane.agent:<6}▌", style=f"bold black on {lane.color}")
        if lane.done:
            line.append("  ✓ " + " ".join(f"#{n}" for n in lane.done), style="green " + ASPHALT)
        rows.append(pad_road(line))

        status = Text("  ", style=ASPHALT)
        elapsed = int(time.time() - lane.started) if lane.current else 0
        detail = lane.phase()
        if lane.current is not None and not lane.waiting_human:
            detail += f" · {elapsed // 60}:{elapsed % 60:02d} · 움직임 {random.randint(1, 9)}초 전"
        status.append(f"        {detail}", style=("bold red " if lane.waiting_human else "grey62 ") + ASPHALT)
        rows.append(pad_road(status))
        rows.append(divider(tick) if i < len(lanes) - 1 else edge())

    working = sum(1 for l in lanes if l.current is not None and not l.waiting_human)
    waiting = sum(1 for l in lanes if l.waiting_human)
    queued = sum(len(l.queue) for l in lanes)
    head = Text()
    head.append(" ▣ 오케스트레이터 ", style="bold white on blue")
    head.append(f"   일하는 중 {working} · ", style="bold")
    head.append(f"나를 기다림 {waiting}", style="bold red" if waiting else "bold")
    head.append(f" · 순서 대기 {queued}", style="bold")
    return Panel(Group(head, Text(""), *rows),
                 title="foragent · 레인 (견본 · 가짜 데이터)", border_style="dim")


def main():
    lanes = [
        Lane("api", "Claude", "orange3", [1, 2, 3, 6]),
        Lane("web", "Codex", "green3", [4, 5, 9]),
        Lane("order", "Codex", "green3", [7, 8]),
        Lane("docs", "Claude", "orange3", []),
    ]
    tick = 0
    with Live(render(lanes, tick), refresh_per_second=4, screen=True) as live:
        while True:
            time.sleep(0.3)
            tick += 1
            if tick % 3 == 0:
                for lane in lanes:
                    lane.step()
            live.update(render(lanes, tick))


if __name__ == "__main__":
    main()
