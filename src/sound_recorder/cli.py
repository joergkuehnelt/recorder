from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from sound_recorder.devices import InputDevice, list_input_devices
from sound_recorder.recorder import build_recorder


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"


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
        raw_value = input(f"Select device [1-{len(devices)}] > ").strip()
        if not raw_value.isdigit():
            print("Enter a device number from the list.")
            continue

        selected_index = int(raw_value)
        for device in devices:
            if device.index == selected_index:
                return device

        print("Selected number is not in the device list.")


def _print_devices(devices: List[InputDevice]) -> None:
    print("=" * 68)
    print(_style(" Available Audio Input Devices", ANSI_BOLD + ANSI_CYAN))
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
    print(_style(" Native macOS Apple Silicon rolling audio recorder", ANSI_BOLD + ANSI_CYAN))
    print("=" * 68)


def _print_session_summary(
    device_name: str,
    output_dir: Path,
    segment_minutes: int,
    arming_duration: float,
    target_peak_dbfs: float,
    warning_peak_dbfs: float,
) -> None:
    print(_style("Recording session", ANSI_BOLD + ANSI_CYAN))
    print("-" * 68)
    print(f" {_style('Device', ANSI_DIM)}        : {device_name}")
    print(f" {_style('Output', ANSI_DIM)}        : {output_dir}")
    print(f" {_style('Segment', ANSI_DIM)}       : {segment_minutes} minute(s)")
    print(f" {_style('Arming', ANSI_DIM)}        : {arming_duration:.1f} second(s)")
    print(f" {_style('Target peak', ANSI_DIM)}   : {_style(f'{target_peak_dbfs:.1f} dBFS', ANSI_GREEN)}")
    print(f" {_style('Warning peak', ANSI_DIM)}  : {_style(f'{warning_peak_dbfs:.1f} dBFS', ANSI_YELLOW)}")
    print(f" {_style('Stop', ANSI_DIM)}          : Ctrl-C after the current file is finalized")
    print("-" * 68)


def _style(text: str, style_code: str) -> str:
    return f"{style_code}{text}{ANSI_RESET}"