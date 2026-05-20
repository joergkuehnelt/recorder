from __future__ import annotations

import os
import re
import select
import shutil
import sys
import termios
import tty
from collections import deque
from typing import List, Optional, Sequence

METER_FLOOR_DBFS = -60.0
# _BAR_WIDTH is computed dynamically; this is the minimum.
_BAR_WIDTH_MIN = 8
_DEVICE_BAR_WIDTH = 16

_BOX_LINES = 6  # top border + 4 content lines + bottom border
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_GREY_LOG_RE = re.compile(r"(\033\[0m)(.+)", re.DOTALL)


def _vis_len(s: str) -> int:
    """Return visible (printed) column width of *s*, ignoring ANSI escapes."""
    return len(_ANSI_RE.sub("", s))


class DashboardInput:
    """Raw-mode stdin reader for arrow-key and Enter navigation."""

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
    """Plain-terminal recorder status display. No alternate screen, no TUI framework."""

    def __init__(self, log_size: int = 10) -> None:
        self._log_lines: deque = deque(maxlen=log_size)
        self._device_names: List[str] = []
        self._device_levels: List[Optional[float]] = []
        self._selected_index: int = 0
        self._device_lines_printed: int = 0
        self._meter_active: bool = False
        self._is_tty: bool = sys.stdout.isatty()

    def __enter__(self) -> RecorderDashboard:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self._end_meter_line()

    # ------------------------------------------------------------------
    # Setup / device-selection phase
    # ------------------------------------------------------------------

    def show_setup_status(self, *lines: str, stage: Optional[str] = None) -> None:
        del stage
        self._end_device_list()
        self._end_meter_line()
        for line in lines:
            print(line, flush=True)

    def set_device_choices(self, device_names: Sequence[str], selected_index: int = 0) -> None:
        self._device_names = list(device_names)
        self._device_levels = [None] * len(device_names)
        self._selected_index = (
            max(0, min(len(device_names) - 1, selected_index)) if device_names else 0
        )
        self._device_lines_printed = 0
        print("Available inputs  (Up/Down to move, Enter to confirm):", flush=True)
        self._redraw_device_list()

    def update_device_levels(self, levels: Sequence[Optional[float]]) -> None:
        self._device_levels = list(levels[: len(self._device_names)])
        while len(self._device_levels) < len(self._device_names):
            self._device_levels.append(None)
        self._redraw_device_list()

    def move_device_selection(self, step: int) -> None:
        if not self._device_names:
            return
        self._selected_index = (self._selected_index + step) % len(self._device_names)
        self._redraw_device_list()

    def selected_device_index(self) -> int:
        return self._selected_index

    # ------------------------------------------------------------------
    # Recording phase
    # ------------------------------------------------------------------

    def begin_recording(self, device_name: str) -> None:
        self._end_device_list()
        self._end_meter_line()
        print(f"\n\033[1;32m● Recording started\033[0m — {device_name}", flush=True)
        print("  Hotkeys: s stop  r restart  q stop-after-finalize\n", flush=True)

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
        term_w = shutil.get_terminal_size(fallback=(80, 24)).columns
        # Keep the box 4 chars narrower than the terminal so we never hit the
        # right margin exactly (that triggers a "pending wrap" in many terminals
        # and inserts a spurious blank line before each box redraw).
        # Box: '+' + '-'*(content_w+2) + '+' = content_w+4 chars wide.
        content_w = max(44, term_w - 6)
        bar_w = max(_BAR_WIDTH_MIN, content_w - 2)  # '[' + bar_w chars + ']'

        # ── Line 1: ● REC (red) + elapsed (amber) ────────────────────────────
        line1 = f"\033[1;31m\u25cf REC\033[0m  \033[33m{elapsed_text}\033[0m"

        # ── Line 2: level meter bar (amber) ──────────────────────────────────
        if gauge_live == 0.0 and gauge_hold == 0.0:
            bar_raw = "[" + "-" * bar_w + "]"
        else:
            bar_raw = _build_bar(gauge_live, gauge_hold, width=bar_w)
        line2 = f"\033[33m{bar_raw}\033[0m"

        # ── Line 3: numeric values (grey) ────────────────────────────────────
        peak_plain = _ANSI_RE.sub("", peak_text)
        hold_plain = _ANSI_RE.sub("", hold_text)
        if alert_text not in {"-", ""}:
            alert_part = f"  \033[1;31m[{alert_text}]\033[0m"
        else:
            alert_part = ""
        line3 = (
            f"\033[90m{peak_plain}  {hold_plain}"
            f"  cpu {cpu_percent}  ram {ram_percent}\033[0m{alert_part}"
        )

        # ── Line 4: hotkeys (S / R / Q with white background) ────────────────
        s_key = "\033[47m\033[30m S \033[0m"
        r_key = "\033[47m\033[30m R \033[0m"
        q_key = "\033[47m\033[30m Q \033[0m"
        line4 = f"{s_key} STOP  {r_key} RESTART  {q_key} SAVE AND QUIT"

        # ── Build ASCII box ───────────────────────────────────────────────────
        border = "+" + "-" * (content_w + 2) + "+"

        def _box_row(content: str) -> str:
            pad = max(0, content_w - _vis_len(content))
            return f"| {content}{' ' * pad} |"

        box = [
            border,
            _box_row(line1),
            _box_row(line2),
            _box_row(line3),
            _box_row(line4),
            border,
        ]

        if self._meter_active:
            sys.stdout.write(f"\033[{_BOX_LINES}A")

        for row in box:
            sys.stdout.write(f"\033[2K\r{row}\n")

        sys.stdout.flush()
        self._meter_active = True

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def log(self, message: str) -> None:
        self._end_device_list()
        self._end_meter_line()
        # Grey out body text after the first ANSI reset (\033[0m) that follows
        # a [TAG] label.  Plain messages (no ANSI codes) are greyed entirely.
        greyed = _GREY_LOG_RE.sub(r"\1\033[90m\2\033[0m", message, count=1)
        if greyed == message:
            greyed = f"\033[90m{message}\033[0m"
        print(greyed, flush=True)

    def prompt_choice(self, title: str, message: str, options: Sequence[str]) -> int:
        self._end_device_list()
        self._end_meter_line()
        print(f"\n{title}", flush=True)
        if message:
            print(message, flush=True)
        selected = 0
        lines_printed = [0]

        def draw() -> None:
            if lines_printed[0]:
                sys.stdout.write(f"\033[{lines_printed[0]}A")
            for i, opt in enumerate(options):
                marker = "> " if i == selected else "  "
                sys.stdout.write(f"\033[2K\r{marker}{opt}\n")
            sys.stdout.flush()
            lines_printed[0] = len(options)

        draw()
        with DashboardInput() as inp:
            while True:
                key = inp.read_key(timeout=0.1)
                if key == "up":
                    selected = (selected - 1) % len(options)
                    draw()
                elif key == "down":
                    selected = (selected + 1) % len(options)
                    draw()
                elif key == "enter":
                    print(flush=True)
                    return selected
        return 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _end_meter_line(self) -> None:
        if self._meter_active:
            # Cursor is already below the box (each box line ends with \n).
            # Just mark the box as no longer active.
            self._meter_active = False

    def _end_device_list(self) -> None:
        # Just mark as consumed; the list lines are already on screen above.
        self._device_lines_printed = 0

    def _redraw_device_list(self) -> None:
        if not self._device_names:
            return
        if self._device_lines_printed and self._is_tty:
            sys.stdout.write(f"\033[{self._device_lines_printed}A")
        for i, name in enumerate(self._device_names):
            selected = i == self._selected_index
            marker = "\033[1;36m>\033[0m" if selected else " "
            dbfs = self._device_levels[i] if i < len(self._device_levels) else None
            label = "   idle" if dbfs is None else f"{dbfs:6.1f} dBFS"
            bar = _build_device_bar(dbfs)
            name_field = name[:42]
            sys.stdout.write(f"\033[2K\r{marker} {name_field:<42s}  {bar}  {label}\n")
        sys.stdout.flush()
        self._device_lines_printed = len(self._device_names)


# ---------------------------------------------------------------------------
# Module-level bar builders
# ---------------------------------------------------------------------------

def _build_bar(live: float, hold: float, width: int = 20) -> str:
    live_index = max(0, min(width - 1, int(round(live * (width - 1)))))
    hold_index = max(0, min(width - 1, int(round(hold * (width - 1)))))
    chars: List[str] = []
    for i in range(width):
        if i < live_index:
            chars.append("=")
        elif i == live_index:
            chars.append(">")
        elif i == hold_index:
            chars.append("|")
        else:
            chars.append("-")
    return "[" + "".join(chars) + "]"


def _build_device_bar(dbfs: Optional[float], width: int = _DEVICE_BAR_WIDTH) -> str:
    if dbfs is None:
        return "[" + "-" * width + "]"
    normalized = max(
        0.0,
        min(1.0, 1.0 - abs(max(METER_FLOOR_DBFS, min(0.0, dbfs))) / abs(METER_FLOOR_DBFS)),
    )
    fill = max(0, min(width, int(round(normalized * width))))
    return "[" + "=" * fill + "-" * (width - fill) + "]"
