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

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


DEVICE_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")
STYLE_AMBER = "color(208)"
STYLE_CYAN = "bold cyan"
STYLE_GREEN = "bold green"
STYLE_YELLOW = "bold yellow"


@dataclass
class DashboardPrompt:
    title: str
    message: str
    options: List[str]
    selected_index: int = 0


@dataclass
class DashboardState:
    stage: str = "device"
    device_names: List[str] = field(default_factory=list)
    selected_device_index: int = 0
    selected_device_name: str = "-"
    status_lines: List[str] = field(default_factory=list)
    title_text: str = "NO DETECTION"
    cpu_percent: str = "0.0%"
    ram_percent: str = "0.0%"
    elapsed_text: str = "00:00"
    peak_text: str = "-60.0 dBFS"
    hold_text: str = "hold -60.0"
    gain_text: str = "1.00"
    alert_text: str = "-"
    gauge_text: str = "[....................................................]"
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=10))
    prompt: Optional[DashboardPrompt] = None
    playlist_candidate_lines: List[str] = field(default_factory=list)


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
        self.live = Live(self._render(), console=self.console, refresh_per_second=20, screen=True)

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
        self.state.selected_device_index = max(0, min(len(device_names) - 1, selected_index)) if device_names else 0
        self.state.status_lines = ["Use Up/Down to select an input device.", "Press Enter to confirm."]
        self.refresh()

    def move_device_selection(self, step: int) -> None:
        if not self.state.device_names:
            return
        count = len(self.state.device_names)
        self.state.selected_device_index = (self.state.selected_device_index + step) % count
        self.refresh()

    def selected_device_index(self) -> int:
        return self.state.selected_device_index

    def show_setup_status(self, *lines: str) -> None:
        self.state.status_lines = list(lines)
        self.refresh()

    def begin_recording(self, device_name: str) -> None:
        self.state.stage = "recording"
        self.state.selected_device_name = device_name
        self.state.prompt = None
        self.state.status_lines = ["Recording dashboard active.", "Hotkeys: s stop, r restart, q stop after finalize."]
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
        gauge_text: str,
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
        self.state.gauge_text = gauge_text
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
        layout = Layout()
        layout.split_column(
            Layout(name="top", ratio=2),
            Layout(name="middle", ratio=2),
            Layout(name="bottom", ratio=2),
        )
        layout["top"].split_row(Layout(name="device"), Layout(name="status"))
        layout["middle"].split_row(Layout(name="gauge"), Layout(name="title"), Layout(name="system"))
        layout["bottom"].split_row(Layout(name="log"))

        layout["device"].update(self._device_panel())
        layout["status"].update(self._status_panel())
        layout["gauge"].update(self._gauge_panel())
        layout["title"].update(self._title_panel())
        layout["system"].update(self._system_panel())
        layout["log"].update(self._log_panel())
        return layout

    def _device_panel(self) -> Panel:
        table = Table.grid(expand=True)
        table.add_column()
        if self.state.device_names:
            for index, name in enumerate(self.state.device_names):
                prefix = "▶ " if index == self.state.selected_device_index and self.state.stage == "device" else "  "
                style = STYLE_CYAN if index == self.state.selected_device_index and self.state.stage == "device" else STYLE_AMBER
                table.add_row(Text(prefix + name, style=style))
        else:
            table.add_row(Text(self.state.selected_device_name or "-", style=STYLE_CYAN))
        return Panel(table, title="Device", border_style="cyan")

    def _status_panel(self) -> Panel:
        body = Group(*(Text(line, style=STYLE_AMBER) for line in self.state.status_lines)) if self.state.status_lines else Text("-", style=STYLE_AMBER)
        if self.state.prompt is not None:
            prompt_table = Table.grid(expand=True)
            prompt_table.add_column()
            prompt_table.add_row(Text(self.state.prompt.message, style=STYLE_AMBER))
            for index, option in enumerate(self.state.prompt.options):
                prefix = "▶ " if index == self.state.prompt.selected_index else "  "
                style = STYLE_CYAN if index == self.state.prompt.selected_index else STYLE_AMBER
                prompt_table.add_row(Text(prefix + option, style=style))
            body = Group(Text(self.state.prompt.title, style=STYLE_GREEN), prompt_table)
        return Panel(body, title="Status", border_style=STYLE_AMBER)

    def _gauge_panel(self) -> Panel:
        return Panel(Align.center(Text(self.state.gauge_text, style=STYLE_GREEN), vertical="middle"), title="Gauge", border_style="green")

    def _title_panel(self) -> Panel:
        style = STYLE_GREEN if self.state.title_text != "NO DETECTION" else STYLE_YELLOW
        return Panel(Align.center(Text(self.state.title_text, style=style), vertical="middle"), title="Title", border_style=STYLE_AMBER)

    def _system_panel(self) -> Panel:
        table = Table.grid(expand=True)
        table.add_column(style=STYLE_CYAN)
        table.add_column(style=STYLE_AMBER)
        table.add_row("Len", self.state.elapsed_text)
        table.add_row("Peak", self.state.peak_text)
        table.add_row("Hold", self.state.hold_text)
        table.add_row("Gain", self.state.gain_text)
        table.add_row("CPU", self.state.cpu_percent)
        table.add_row("RAM", self.state.ram_percent)
        table.add_row("Alert", self.state.alert_text)
        return Panel(table, title="System", border_style=STYLE_AMBER)

    def _log_panel(self) -> Panel:
        lines = list(self.state.log_lines) or ["No log messages yet."]
        body = Group(*(Text(line, style=STYLE_AMBER) for line in lines))
        return Panel(body, title="Log", border_style=STYLE_AMBER)