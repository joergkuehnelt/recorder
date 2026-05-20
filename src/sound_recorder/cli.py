from __future__ import annotations

import argparse
import platform
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, List

from rich.console import Console
from rich.table import Table

from sound_recorder.dashboard import DashboardInput, RecorderDashboard
from sound_recorder.playlist import maybe_start_playlist_companion

if TYPE_CHECKING:
    from sound_recorder.devices import InputDevice


PLAYLIST_CANDIDATE_RE = re.compile(r"^\s*(\d+)\.\s+(.*)$")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than 0.")
    return parsed


def _negative_dbfs(value: str) -> float:
    parsed = float(value)
    if parsed >= 0:
        raise argparse.ArgumentTypeError("Value must be below 0 dBFS.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sound-recorder",
        description="Record rolling audio segments on macOS using AVFoundation.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio input devices and exit.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("recordings"),
        help="Directory for finished recordings.",
    )
    parser.add_argument(
        "--segment-minutes",
        type=int,
        default=60,
        help="Length of each recording segment in minutes.",
    )
    parser.add_argument(
        "--arming-duration",
        type=_positive_float,
        default=3.0,
        help="Seconds used for pre-roll arming and level calibration.",
    )
    parser.add_argument(
        "--target-peak-dbfs",
        type=_negative_dbfs,
        default=-9.0,
        help="Target peak level during arming, in dBFS.",
    )
    parser.add_argument(
        "--warning-peak-dbfs",
        type=_negative_dbfs,
        default=-3.0,
        help="Peak warning threshold during recording, in dBFS.",
    )
    return parser


def main() -> int:
    _ensure_supported_runtime()

    from sound_recorder.devices import list_input_devices
    from sound_recorder.recorder import build_recorder

    args = build_parser().parse_args()
    if args.warning_peak_dbfs <= args.target_peak_dbfs:
        raise SystemExit("--warning-peak-dbfs must be higher than --target-peak-dbfs.")

    devices = list_input_devices()
    if not devices:
        raise SystemExit("No audio input devices were found.")

    if args.list_devices:
        _render_device_list(devices)
        return 0

    with RecorderDashboard(log_size=10) as dashboard:
        dashboard.show_setup_status(
            "Checking playlist helper state.",
            "A remembered helper is started automatically when it is not already running.",
            stage="setup",
        )
        _maybe_start_playlist_helper(dashboard)
        dashboard.show_setup_status(
            "Scanning live input levels.",
            "Choose your input with Up/Down and Enter. The recording dashboard opens after selection.",
            stage="device",
        )
        device = _select_device(devices, dashboard)

        recorder = build_recorder(
            device_id=device.unique_id,
            output_dir=args.output_dir,
            segment_minutes=args.segment_minutes,
            arming_duration_seconds=args.arming_duration,
            target_peak_dbfs=args.target_peak_dbfs,
            warning_peak_dbfs=args.warning_peak_dbfs,
        )
        recorder.set_dashboard(dashboard)
        dashboard.begin_recording(device.name)
        dashboard.log(f"Output: {args.output_dir.expanduser().resolve()}")
        dashboard.log(
            f"Segment {args.segment_minutes} min  "
            f"Arming {args.arming_duration:.1f}s  "
            f"Target {args.target_peak_dbfs:.0f} dBFS  "
            f"Warn {args.warning_peak_dbfs:.0f} dBFS"
        )
        try:
            recorder.run()
        except Exception as exc:  # noqa: BLE001
            dashboard.log(f"\033[1;31m[FATAL]\033[0m {exc}")
            return 1
    return 0


def _select_device(devices: List[InputDevice], dashboard: RecorderDashboard) -> InputDevice:
    from sound_recorder.devices import InputLevelMonitor

    dashboard.set_device_choices([device.name for device in devices])
    with InputLevelMonitor([device.unique_id for device in devices]) as level_monitor:
        with DashboardInput() as dashboard_input:
            while True:
                levels = level_monitor.poll_levels()
                dashboard.update_device_levels([levels.get(device.unique_id) for device in devices])
                key = dashboard_input.read_key(timeout=0.08)
                if key == "up":
                    dashboard.move_device_selection(-1)
                elif key == "down":
                    dashboard.move_device_selection(1)
                elif key == "enter":
                    selected = devices[dashboard.selected_device_index()]
                    dashboard.log(f"Selected input: {selected.name}")
                    return selected


def _render_device_list(devices: List[InputDevice]) -> None:
    console = Console()
    table = Table(title="Available Audio Input Devices")
    table.add_column("#", style="bold cyan", no_wrap=True)
    table.add_column("Name", style="bold green")
    table.add_column("Model", style="color(208)")
    table.add_column("UID", style="white")
    for device in devices:
        table.add_row(str(device.index), device.name, device.model_id or "no model id", device.unique_id)
    console.print(table)


def _maybe_start_playlist_helper(dashboard: RecorderDashboard) -> None:
    adapter = _DashboardPlaylistAdapter(dashboard)
    try:
        result = maybe_start_playlist_companion(
            input_func=adapter.input_func,
            print_func=adapter.print_func,
        )
    except Exception as exc:
        dashboard.log(f"Playlist helper skipped: {exc}")
        return

    if result.status_message:
        dashboard.log(result.status_message)
    if result.last_entry:
        dashboard.log(f"Last track: {result.last_entry}")
    if result.last_state_entry and not result.last_state_entry.endswith("=> NO DETECTION"):
        dashboard.log(f"Last state: {result.last_state_entry}")


class _DashboardPlaylistAdapter:
    def __init__(self, dashboard: RecorderDashboard) -> None:
        self.dashboard = dashboard
        self.candidate_lines: List[str] = []

    def print_func(self, *args, sep: str = " ", end: str = "\n") -> None:
        del end
        text = sep.join(str(arg) for arg in args)
        if text.startswith("Detected playlist script candidates"):
            self.candidate_lines = []
        match = PLAYLIST_CANDIDATE_RE.match(text)
        if match is not None:
            self.candidate_lines.append(match.group(2))
        self.dashboard.log(text)

    def input_func(self, prompt: str) -> str:
        if prompt.startswith("Search Documents for a playlist helper script"):
            index = self.dashboard.prompt_choice("Playlist Helper", prompt.strip(), ["No", "Yes"])
            return "n" if index == 0 else "y"
        if "[Y]es / [N]o / [C]hoose path" in prompt:
            index = self.dashboard.prompt_choice(
                "Playlist Helper",
                "Remembered playlist helper found.",
                ["Yes", "No", "Choose Path"],
            )
            return ["y", "n", "c"][index]
        if "[Y/n]" in prompt:
            index = self.dashboard.prompt_choice("Playlist Helper", prompt.strip(), ["Yes", "No"])
            return "y" if index == 0 else "n"
        if prompt.startswith("Select script"):
            options = self.candidate_lines + ["Skip"]
            index = self.dashboard.prompt_choice("Playlist Helper", "Select a playlist helper script.", options)
            if index == len(options) - 1:
                return "0"
            return str(index + 1)
        return ""


def _ensure_supported_runtime() -> None:
    if sys.platform != "darwin":
        raise SystemExit("sound-recorder requires macOS.")

    machine = platform.machine()
    if machine != "arm64":
        raise SystemExit(
            f"sound-recorder requires native arm64 Python on Apple Silicon, got {machine}."
        )

    try:
        import AVFoundation  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "AVFoundation is unavailable. Use the project bootstrap on macOS with PyObjC installed."
        ) from exc