from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from sound_recorder.devices import InputDevice, list_input_devices
from sound_recorder.playlist import (
    build_amber_box_lines,
    build_green_status_line,
    maybe_start_playlist_companion,
)
from sound_recorder.recorder import build_recorder


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_AMBER = "\033[38;5;208m"


BANNER = r"""
    ____                            _
 / ___|  ___  _   _ _ __   __| |
 \___ \ / _ \| | | | '_ \ / _` |
    ___) | (_) | |_| | | | | (_| |
 |____/ \___/ \__,_|_| |_|\__,_|

    ____                              _
 |  _ \ ___  ___ ___  _ __ __| | ___ _ __
 | |_) / _ \/ __/ _ \| '__/ _` |/ _ \ '__|
 |  _ <  __/ (_| (_) | | | (_| |  __/ |
 |_| \_\___|\___\___/|_|  \__,_|\___|_|
""".strip("\n")


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
    args = build_parser().parse_args()
    if args.warning_peak_dbfs <= args.target_peak_dbfs:
        raise SystemExit("--warning-peak-dbfs must be higher than --target-peak-dbfs.")

    _print_banner()
    devices = list_input_devices()

    if not devices:
        raise SystemExit("No audio input devices were found.")

    if args.list_devices:
        _print_devices(devices)
        return 0

    device = _select_device(devices)
    _maybe_start_playlist_helper()
    recorder = build_recorder(
        device_id=device.unique_id,
        output_dir=args.output_dir,
        segment_minutes=args.segment_minutes,
        arming_duration_seconds=args.arming_duration,
        target_peak_dbfs=args.target_peak_dbfs,
        warning_peak_dbfs=args.warning_peak_dbfs,
    )

    _print_session_summary(
        device_name=device.name,
        output_dir=args.output_dir.expanduser().resolve(),
        segment_minutes=args.segment_minutes,
        arming_duration=args.arming_duration,
        target_peak_dbfs=args.target_peak_dbfs,
        warning_peak_dbfs=args.warning_peak_dbfs,
    )
    recorder.run()
    return 0


def _select_device(devices: List[InputDevice]) -> InputDevice:
    _print_devices(devices)
    while True:
        raw_value = input(_style(f"[INFO] Select device [1-{len(devices)}] > ", ANSI_BOLD + ANSI_CYAN)).strip()
        if not raw_value.isdigit():
            _print_tagged_line("WARN", "Enter a device number from the list.", ANSI_YELLOW)
            continue

        selected_index = int(raw_value)
        for device in devices:
            if device.index == selected_index:
                _print_tagged_line("OK", f"Selected input: {device.name}", ANSI_GREEN)
                return device

        _print_tagged_line("WARN", "Selected number is not in the device list.", ANSI_YELLOW)


def _print_devices(devices: List[InputDevice]) -> None:
    print("=" * 68)
    _print_tagged_line("INFO", "Available audio input devices", ANSI_CYAN)
    print("=" * 68)
    for device in devices:
        details = device.model_id or "no model id"
        print(f" {_style(f'{device.index:>2}.', ANSI_BOLD + ANSI_GREEN)} {device.name}")
        print(f"     {_style('model', ANSI_DIM)} : {details}")
        print(f"     {_style('uid', ANSI_DIM)}   : {device.unique_id}")
        print(f"     {_style('hint', ANSI_YELLOW)}  : select {device.index} to use this input")
    print("=" * 68)


def _print_banner() -> None:
    print(_style(BANNER, ANSI_CYAN))
    print("=" * 68)
    _print_tagged_line("INFO", "Native macOS Apple Silicon rolling audio recorder", ANSI_CYAN)
    print("=" * 68)


def _maybe_start_playlist_helper() -> None:
    try:
        result = maybe_start_playlist_companion()
    except Exception as exc:
        print(_style(f"[SKIP] Playlist helper skipped: {exc}", ANSI_BOLD + ANSI_YELLOW))
        return

    if result.status_message:
        status_color = ANSI_CYAN
        status_label = "[INFO]"
        if result.status_kind in {"skip", "warning"}:
            status_color = ANSI_YELLOW
            status_label = "[SKIP]" if result.status_kind == "skip" else "[WARN]"
        elif result.status_kind == "success":
            status_color = ANSI_GREEN
            status_label = "[OK]"
        print(_style(f"{status_label} {result.status_message}", ANSI_BOLD + status_color))

    if not result.started:
        return

    if result.last_entry:
        for line in build_amber_box_lines(result.last_entry):
            print(_style(line, ANSI_BOLD + ANSI_AMBER))

    if result.last_state_entry:
        print(_style(build_green_status_line(result.last_state_entry), ANSI_BOLD + ANSI_GREEN))


def _print_session_summary(
    device_name: str,
    output_dir: Path,
    segment_minutes: int,
    arming_duration: float,
    target_peak_dbfs: float,
    warning_peak_dbfs: float,
) -> None:
    _print_tagged_line("INFO", "Recording session", ANSI_CYAN)
    print("-" * 68)
    print(f" {_style('Device', ANSI_DIM)}        : {device_name}")
    print(f" {_style('Output', ANSI_DIM)}        : {output_dir}")
    print(f" {_style('Segment', ANSI_DIM)}       : {segment_minutes} minute(s)")
    print(f" {_style('Arming', ANSI_DIM)}        : {arming_duration:.1f} second(s)")
    print(f" {_style('Target peak', ANSI_DIM)}   : {_style(f'{target_peak_dbfs:.1f} dBFS', ANSI_GREEN)}")
    print(f" {_style('Warning peak', ANSI_DIM)}  : {_style(f'{warning_peak_dbfs:.1f} dBFS', ANSI_YELLOW)}")
    print(f" {_style('Stop', ANSI_DIM)}          : Ctrl-C after the current file is finalized")
    print("-" * 68)


def _print_tagged_line(label: str, message: str, color: str) -> None:
    print(_style(f"[{label}] {message}", ANSI_BOLD + color))


def _style(text: str, style_code: str) -> str:
    return f"{style_code}{text}{ANSI_RESET}"