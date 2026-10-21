"""Throwaway mock of the Foragent lane conveyor TUI (fake data, no Foragent calls).

Run: uv run --quiet --with rich python docs/impl/conveyor_mock.py
"""

import random
import time

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

PICK = ["  ⛏ ", " ⛏  ", "⛏✦  ", " ⛏  "]
SLEEP = ["  z ", "  zZ", " zZz", "  zZ"]
HAND = [" ✋ ", " ✋!", " ✋ ", " ✋!"]
ROAD_WIDTH = 54
ASPHALT = "on grey15"
DEPLOY_TICKS = 4  # ticks between agents arriving at their lanes

TITLES = {
    1: "Login API", 2: "Token refresh", 3: "Payment API", 4: "Login page", 5: "Error message",
    6: "Rate limit", 7: "Interview engine setting", 8: "/fa-order interview branch", 9: "Payment page",
}


class Lane:
    def __init__(self, name, emoji, agent, executor, model, skills, issues):
        self.name, self.emoji, self.agent, self.executor, self.model, self.skills = name, emoji, agent, executor, model, skills
        self.color = "orange3" if executor == "Claude" else "green3"
        self.badge = "✳" if executor == "Claude" else "✺"
        self.queue = issues
        self.current = None
        self.progress = 0
        self.started = time.time()
        self.last_move = time.time()
        self.attempt = 1
        self.done = []
        self.waiting_human = False
        self.deployed = False

    def step(self):
        if not self.deployed or self.waiting_human:
            return
        if self.current is None and self.queue:
            self.current = self.queue.pop(0)
            self.progress = 0
            self.attempt = 1
            self.started = time.time()
        if self.current is not None:
            self.progress += random.randint(4, 11)
            self.last_move = time.time() - random.randint(0, 8)
            if self.current == 7 and self.progress > 60:
                self.waiting_human = True
                return
            if self.progress >= 100:
                self.done.append(self.current)
                self.current = None

    def phase(self):
        if not self.deployed:
            return "배치 중"
        if self.waiting_human:
            return "오케스트레이터 확인 중"
        if self.current is None:
            return "할 일 없음"
        p = self.progress
        return "구현" if p < 55 else "확인 명령" if p < 75 else "리뷰" if p < 92 else "머지 대기"


def edge():
    return Text("━" * ROAD_WIDTH, style="bold white")


def divider():
    return Text(("▬▬▬   " * (ROAD_WIDTH // 6 + 1))[:ROAD_WIDTH], style="yellow " + ASPHALT)


def pad(line):
    gap = ROAD_WIDTH - line.cell_len
    if gap > 0:
        line.append(" " * gap, style=ASPHALT)
    return line


def road(lanes, tick):
    rows = [edge()]
    for i, lane in enumerate(lanes):
        line = Text(f" {lane.name:<6}", style="bold white " + ASPHALT)
        line.append("▸ ", style="dim " + ASPHALT)
        queue = "".join(f"[#{n}] " for n in reversed(lane.queue))
        line.append(f"{queue:>16}", style="white " + ASPHALT)
        if lane.current is not None:
            line.append(f"[#{lane.current}]", style=f"bold {lane.color} " + ASPHALT)
        else:
            line.append("    ", style=ASPHALT)
        if not lane.deployed:
            tool, label_style = "    ", "dim " + ASPHALT
        elif lane.waiting_human:
            tool, label_style = HAND[tick % 4], f"bold black on {lane.color}"
        elif lane.current is None:
            tool, label_style = SLEEP[tick % 4], f"bold black on {lane.color}"
        else:
            tool, label_style = PICK[tick % 4], f"bold black on {lane.color}"
        line.append(tool, style="yellow " + ASPHALT)
        line.append(" ", style=ASPHALT)
        if lane.deployed:
            line.append(f"▐ {lane.badge} {lane.emoji} {lane.agent:<9}▌", style=label_style)
        else:
            line.append("  · · ·  배치 중  ", style=label_style)
        rows.append(pad(line))
        sub = Text(" " * 8, style=ASPHALT)
        if lane.done:
            last = TITLES.get(lane.done[-1], f"#{lane.done[-1]}")
            sub.append(f"✓ {len(lane.done)}개 끝남 · 방금: {last}", style="green " + ASPHALT)
        rows.append(pad(sub))
        rows.append(divider() if i < len(lanes) - 1 else edge())
    return Group(*rows)


def orchestrator(lanes, tick):
    triage = sum(1 for l in lanes if l.waiting_human)
    pending = [l for l in lanes if not l.deployed]
    body = Text()
    body.append("🧭 오케스트레이터\n\n", style="bold white")
    if pending:
        body.append(f"에이전트 배치 중\n→ {pending[0].agent}\n", style="cyan")
    elif triage:
        body.append("문제 확인 중\n", style="bold yellow")
    else:
        body.append("지켜보는 중\n", style="green")
    body.append("\n나를 기다림 ", style="bold")
    body.append("0", style="bold")
    links = Text("\n")
    for lane in lanes:
        links.append("━━━━▶\n\n\n", style="bold blue" if lane.deployed else "dim")
    grid = Table.grid(padding=0)
    grid.add_column(width=18)
    grid.add_column(width=6)
    grid.add_row(Panel(body, border_style="blue", height=13), links)
    return grid


def pulse(lane, tick):
    """One liveness signal instead of raw numbers."""
    if lane.current is None or not lane.deployed:
        return Text("")
    if lane.waiting_human:
        return Text("● 오케스트레이터가 보는 중", style="yellow")
    idle = int(time.time() - lane.last_move)
    if idle > 600:
        return Text(f"○ 멈춘 듯 {idle // 60}분", style="bold red")
    return Text("● 움직이는 중", style="green" if tick % 2 else "dark_green")


def work_table(lanes, tick):
    table = Table(expand=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("레인", no_wrap=True)
    table.add_column("지금 하는 일", no_wrap=True, overflow="ellipsis", ratio=3)
    table.add_column("단계", no_wrap=True)
    table.add_column("", no_wrap=True)
    for lane in lanes:
        if lane.current is not None and lane.deployed:
            task = TITLES.get(lane.current, f"#{lane.current}")
            if lane.attempt > 1:
                task += f"  (재시도 {lane.attempt})"
        else:
            task = "—"
        table.add_row(Text(f"{lane.emoji} {lane.name}", style=f"bold {lane.color}"), task,
                      Text(lane.phase(), style="bold yellow" if lane.waiting_human else ""), pulse(lane, tick))
    return table


def render(lanes, tick):
    top = Table.grid(padding=0)
    top.add_column()
    top.add_column()
    top.add_row(orchestrator(lanes, tick), road(lanes, tick))
    layout = Layout()
    layout.split_column(
        Layout(Panel(top, title="foragent · 레인 (견본 · 가짜 데이터)", border_style="dim"), size=17),
        Layout(Panel(work_table(lanes, tick), title="지금 하는 일", border_style="dim"), size=8),
        Layout(Text(" ")),
    )
    return layout


def main():
    lanes = [
        Lane("api", "🔧", "fa-api", "Claude", "opus-5.5 · high", ["testing-strategy", "system-design"], [1, 2, 3, 6]),
        Lane("web", "🎨", "fa-web", "Codex", "gpt-6.1 · xhigh", ["frontend-design", "accessibility-review", "ux-copy"], [4, 5, 9]),
        Lane("order", "📋", "fa-order", "Codex", "gpt-6.1 · medium", [], [7, 8]),
        Lane("docs", "📝", "fa-docs", "Claude", "sonnet-5.5 · low", ["documentation"], []),
    ]
    tick = 0
    with Live(render(lanes, tick), refresh_per_second=4, screen=True) as live:
        while True:
            time.sleep(0.3)
            tick += 1
            for i, lane in enumerate(lanes):
                if not lane.deployed and tick >= (i + 1) * DEPLOY_TICKS:
                    lane.deployed = True
            if tick % 3 == 0:
                for lane in lanes:
                    lane.step()
            live.update(render(lanes, tick))


if __name__ == "__main__":
    main()
