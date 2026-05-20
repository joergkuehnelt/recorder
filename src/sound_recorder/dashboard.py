from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


STYLE_AMBER = "color(214)"
STYLE_CYAN = "bold cyan"
STYLE_GREEN = "bold green"
STYLE_YELLOW = "bold yellow"
STYLE_RED = "bold red"
STYLE_DIM = "grey62"
METER_FLOOR_DBFS = -60.0
DEVICE_METER_WIDTH = 18


@dataclass
class DashboardPrompt:
    title: str
    message: str
    options: List[str]
    selected_index: int = 0


@dataclass
class DashboardState:
    stage: str = "setup"
    device_names: List[str] = field(default_factory=list)
    device_levels: List[Optional[float]] = field(default_factory=list)
    selected_device_index: int = 0
    selected_device_name: str = "-"
    status_lines: List[str] = field(default_factory=list)
    title_text: str = "NO DETECTION"
    cpu_percent: str = "0.0%"
    ram_percent: str = "0.0%"
    elapsed_text: str = "00:00"
    peak_text: str = "-60.0 dBFS"
    hold_text: str = "hold -60.0 dBFS"
    gain_text: str = "1.00"
    alert_text: str = "-"
    gauge_live: float = 0.0
    gauge_hold: float = 0.0
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    prompt: Optional[DashboardPrompt] = None


class DashboardInput:
    def __init__(self) -> None:
        self.enabled = sys.stdin.isatty()
        self.stdin_fd: Optional[int] = None
        self.stdin_termios_state = None

    def __enter__(self) -> DashboardInput:
        if not self.enabled:
            return self
        try:
            self.stdin_fd = sys.stdin.fileno()
            self.stdin_termios_state = termios.tcgetattr(self.stdin_fd)
            tty.setcbreak(self.stdin_fd)
        except (termios.error, ValueError, OSError):
            self.enabled = False
            self.stdin_fd = None
            self.stdin_termios_state = None
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        if not self.enabled or self.stdin_fd is None or self.stdin_termios_state is None:
            return
        try:
            termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.stdin_termios_state)
        except (termios.error, ValueError, OSError):
            pass
        finally:
            self.stdin_fd = None
            self.stdin_termios_state = None

    def read_key(self, timeout: float = 0.1) -> Optional[str]:
        if not self.enabled or self.stdin_fd is None:
            return None
        try:
            readable, _, _ = select.select([self.stdin_fd], [], [], timeout)
        except (OSError, ValueError):
            self.enabled = False
            return None
        if not readable:
            return None
        try:
            payload = os.read(self.stdin_fd, 3)
        except OSError:
            self.enabled = False
            return None
        if payload == b"\x1b[A":
            return "up"
        if payload == b"\x1b[B":
            return "down"
        if payload in {b"\r", b"\n"}:
            return "enter"
        if payload[:1] == b"\x1b":
            return None
        return payload[:1].decode("utf-8", errors="ignore").lower() or None


class RecorderDashboard:
    def __init__(self, log_size: int = 10) -> None:
        self.console = Console()
        self.state = DashboardState(log_lines=deque(maxlen=log_size))
        self.live = Live(self._render(), console=self.console, refresh_per_second=30, screen=True)

    def __enter__(self) -> RecorderDashboard:
        self.live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.live.stop()

    def refresh(self) -> None:
        self.live.update(self._render(), refresh=True)

    def log(self, message: str) -> None:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", message).strip()
        if plain:
            self.state.log_lines.appendleft(plain)
        self.refresh()

    def set_device_choices(self, device_names: Sequence[str], selected_index: int = 0) -> None:
        self.state.stage = "device"
        self.state.device_names = list(device_names)
        self.state.device_levels = [None for _ in self.state.device_names]
        self.state.selected_device_index = max(0, min(len(device_names) - 1, selected_index)) if device_names else 0
        self.state.status_lines = [
            "Choose the input with Up/Down and confirm with Enter.",
            "Every source shows its live input level before recording starts.",
        ]
        self.refresh()

    def update_device_levels(self, levels: Sequence[Optional[float]]) -> None:
        self.state.device_levels = list(levels[: len(self.state.device_names)])
        while len(self.state.device_levels) < len(self.state.device_names):
            self.state.device_levels.append(None)
        self.refresh()

    def move_device_selection(self, step: int) -> None:
        if not self.state.device_names:
            return
        count = len(self.state.device_names)
        self.state.selected_device_index = (self.state.selected_device_index + step) % count
        self.refresh()

    def selected_device_index(self) -> int:
        return self.state.selected_device_index

    def show_setup_status(self, *lines: str, stage: Optional[str] = None) -> None:
        if stage is not None:
            self.state.stage = stage
        self.state.status_lines = list(lines)
        self.refresh()

    def begin_recording(self, device_name: str) -> None:
        self.state.stage = "recording"
        self.state.selected_device_name = device_name
        self.state.prompt = None
        self.state.status_lines = [
            f"Input: {device_name}",
            "Hotkeys: s stop, r restart, q stop after finalize.",
        ]
        self.refresh()

    def update_recording(
        self,
        *,
        elapsed_text: str,
        peak_text: str,
        hold_text: str,
        gain_text: str,
        alert_text: str,
        title_text: str,
        cpu_percent: str,
        ram_percent: str,
        gauge_live: float,
        gauge_hold: float,
        status_lines: Sequence[str],
    ) -> None:
        self.state.stage = "recording"
        self.state.elapsed_text = elapsed_text
        self.state.peak_text = peak_text
        self.state.hold_text = hold_text
        self.state.gain_text = gain_text
        self.state.alert_text = alert_text
        self.state.title_text = title_text
        self.state.cpu_percent = cpu_percent
        self.state.ram_percent = ram_percent
        self.state.gauge_live = max(0.0, min(1.0, gauge_live))
        self.state.gauge_hold = max(0.0, min(1.0, gauge_hold))
        self.state.status_lines = list(status_lines)
        self.refresh()

    def prompt_choice(self, title: str, message: str, options: Sequence[str]) -> int:
        self.state.prompt = DashboardPrompt(title=title, message=message, options=list(options))
        self.refresh()
        with DashboardInput() as dashboard_input:
            while True:
                key = dashboard_input.read_key(timeout=0.1)
                if key == "up":
                    self.state.prompt.selected_index = (self.state.prompt.selected_index - 1) % len(self.state.prompt.options)
                    self.refresh()
                elif key == "down":
                    self.state.prompt.selected_index = (self.state.prompt.selected_index + 1) % len(self.state.prompt.options)
                    self.refresh()
                elif key == "enter":
                    selected_index = self.state.prompt.selected_index
                    self.state.prompt = None
                    self.refresh()
                    return selected_index

    def _render(self) -> RenderableType:
        if self.state.stage == "recording":
            return self._render_recording_layout()
        return self._render_setup_layout()

    def _render_setup_layout(self) -> RenderableType:
        layout = Layout()
        layout.split_column(
            Layout(name="top", ratio=3),
            Layout(name="log", size=12),
        )
        layout["top"].split_row(
            Layout(name="device", ratio=3),
            Layout(name="status", ratio=2),
        )
        layout["device"].update(self._device_panel())
        layout["status"].update(self._status_panel())
        layout["log"].update(self._log_panel())
        return layout

    def _render_recording_layout(self) -> RenderableType:
        layout = Layout()
        layout.split_column(
            Layout(name="gauge", size=8),
            Layout(name="middle", ratio=2),
            Layout(name="log", size=12),
        )
        layout["middle"].split_row(
            Layout(name="status", ratio=4),
            Layout(name="system", ratio=2),
            Layout(name="title", ratio=3),
        )
        layout["gauge"].update(self._gauge_panel())
        layout["status"].update(self._status_panel())
        layout["system"].update(self._system_panel())
        layout["title"].update(self._title_panel())
        layout["log"].update(self._log_panel())
        return layout

    def _device_panel(self) -> Panel:
        if not self.state.device_names:
            empty = Align.center(Text("Preparing input sources...", style=STYLE_DIM), vertical="middle")
            return Panel(empty, title="Input Sources", border_style=STYLE_CYAN, box=box.ROUNDED)

        table = Table.grid(expand=True)
        table.add_column(ratio=4)
        table.add_column(ratio=3)
        table.add_column(justify="right", no_wrap=True)
        for index, name in enumerate(self.state.device_names):
            selected = self.state.stage == "device" and index == self.state.selected_device_index
            style = STYLE_CYAN if selected else STYLE_AMBER
            prefix = "> " if selected else "  "
            dbfs = self.state.device_levels[index] if index < len(self.state.device_levels) else None
            label = "idle" if dbfs is None else f"{dbfs:5.1f} dBFS"
            meter = self._build_device_meter(dbfs, selected=selected)
            table.add_row(Text(prefix + name, style=style), meter, Text(label, style=style))

        return Panel(
            table,
            title="Input Sources",
            subtitle="Arrows move, Enter confirms",
            border_style=STYLE_CYAN,
            box=box.HEAVY,
        )

    def _status_panel(self) -> Panel:
        if self.state.prompt is not None:
            prompt_table = Table.grid(expand=True)
            prompt_table.add_column()
            prompt_table.add_row(Text(self.state.prompt.message, style=STYLE_AMBER))
            prompt_table.add_row(Text("", style=STYLE_DIM))
            for index, option in enumerate(self.state.prompt.options):
                selected = index == self.state.prompt.selected_index
                prompt_table.add_row(Text(("> " if selected else "  ") + option, style=STYLE_CYAN if selected else STYLE_AMBER))
            body: RenderableType = Group(Text(self.state.prompt.title, style=STYLE_GREEN), prompt_table)
        elif self.state.status_lines:
            body = Group(*(Text(line, style=STYLE_AMBER) for line in self.state.status_lines))
        else:
            body = Text("-", style=STYLE_DIM)

        return Panel(body, title="Status", border_style=STYLE_AMBER, box=box.ROUNDED)

    def _gauge_panel(self) -> Panel:
        width = max(48, self.console.size.width - 14)
        scale_text = self._build_scale_text(width)
        live_text = self._build_recording_meter(width, self.state.gauge_live, self.state.gauge_hold)
        footer = Text()
        footer.append("peak ", style=STYLE_DIM)
        footer.append(self.state.peak_text, style=self._alert_style())
        footer.append("   hold ", style=STYLE_DIM)
        footer.append(self.state.hold_text.replace("hold ", ""), style=STYLE_CYAN)
        footer.append("   gain ", style=STYLE_DIM)
        footer.append(self.state.gain_text, style=STYLE_GREEN)
        footer.append("   alert ", style=STYLE_DIM)
        footer.append(self.state.alert_text, style=self._alert_style())
        body = Group(scale_text, live_text, footer)
        return Panel(
            body,
            title="Signal",
            subtitle="Reactive peak gauge with short hold",
            border_style=self._alert_style(),
            box=box.DOUBLE,
        )

    def _title_panel(self) -> Panel:
        style = STYLE_GREEN if self.state.title_text != "NO DETECTION" else STYLE_YELLOW
        body = Align.center(Text(self.state.title_text, style=style, justify="center"), vertical="middle")
        return Panel(body, title="Title", border_style=style, box=box.HEAVY)

    def _system_panel(self) -> Panel:
        table = Table.grid(expand=True)
        table.add_column(style=STYLE_CYAN)
        table.add_column(style=STYLE_AMBER, justify="right")
        table.add_row("Len", self.state.elapsed_text)
        table.add_row("Peak", self.state.peak_text)
        table.add_row("Hold", self.state.hold_text.replace("hold ", ""))
        table.add_row("Gain", self.state.gain_text)
        table.add_row("CPU", self.state.cpu_percent)
        table.add_row("RAM", self.state.ram_percent)
        table.add_row("Alert", self.state.alert_text)
        return Panel(table, title="System", border_style=STYLE_AMBER, box=box.ROUNDED)

    def _log_panel(self) -> Panel:
        lines = list(self.state.log_lines) or ["No log messages yet."]
        body = Group(*(Text(line, style=STYLE_AMBER if index == 0 else STYLE_DIM) for index, line in enumerate(lines)))
        return Panel(body, title="Log", border_style=STYLE_AMBER, box=box.ROUNDED)

    def _build_device_meter(self, dbfs: Optional[float], selected: bool) -> Text:
        width = DEVICE_METER_WIDTH
        normalized = 0.0 if dbfs is None else self._normalized_from_dbfs(dbfs)
        live_index = max(0, min(width - 1, int(round(normalized * (width - 1)))))
        meter = Text("[", style=STYLE_DIM)
        for index in range(width):
            zone_style = self._zone_style(index, width)
            if dbfs is None:
                meter.append("-", style=STYLE_DIM)
            elif index < live_index:
                meter.append("=", style=zone_style)
            elif index == live_index:
                meter.append(">", style="bold black on cyan" if selected else "bold black on white")
            else:
                meter.append("-", style=STYLE_DIM)
        meter.append("]", style=STYLE_DIM)
        return meter

    def _build_recording_meter(self, width: int, live: float, hold: float) -> Text:
        live_index = max(0, min(width - 1, int(round(live * (width - 1)))))
        hold_index = max(0, min(width - 1, int(round(hold * (width - 1)))))
        meter = Text("[", style=STYLE_DIM)
        for index in range(width):
            zone_style = self._zone_style(index, width)
            if index < live_index:
                meter.append("=", style=zone_style)
            elif index == live_index:
                meter.append(">", style="bold black on cyan")
            elif index == hold_index:
                meter.append("|", style=STYLE_CYAN)
            else:
                meter.append("-", style=STYLE_DIM)
        meter.append("]", style=STYLE_DIM)
        return meter

    def _build_scale_text(self, width: int) -> Text:
        tick_labels = ["-60", "-36", "-24", "-12", "-6", "0 dBFS"]
        if width <= len(tick_labels[-1]):
            return Text(" ".join(tick_labels), style=STYLE_DIM)
        columns = [int(round(index * (width - 1) / (len(tick_labels) - 1))) for index in range(len(tick_labels))]
        chars = [" " for _ in range(width)]
        for column, label in zip(columns, tick_labels):
            start = max(0, min(width - len(label), column - (len(label) // 2)))
            for offset, char in enumerate(label):
                chars[start + offset] = char
        return Text("".join(chars), style=STYLE_DIM)

    @staticmethod
    def _normalized_from_dbfs(dbfs: float) -> float:
        clamped = max(METER_FLOOR_DBFS, min(0.0, dbfs))
        return max(0.0, min(1.0, 1.0 - (abs(clamped) / abs(METER_FLOOR_DBFS))))

    def _zone_style(self, index: int, width: int) -> str:
        if index >= int(round(width * 0.88)):
            return STYLE_RED
        if index >= int(round(width * 0.68)):
            return STYLE_YELLOW
        return STYLE_GREEN

    def _alert_style(self) -> str:
        if self.state.alert_text == "CLIP":
            return STYLE_RED
        if self.state.alert_text == "HOT":
            return STYLE_YELLOW
        return STYLE_GREEN