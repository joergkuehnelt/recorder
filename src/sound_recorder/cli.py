from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from sound_recorder.devices import InputDevice, list_input_devices
from sound_recorder.recorder import build_recorder


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
    return parser


def main() -> int:
    args = build_parser().parse_args()
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
    )

    print(f"Using input device: {device.name}")
    print(f"Saving recordings to: {args.output_dir.expanduser().resolve()}")
    print(f"Segment length: {args.segment_minutes} minute(s)")
    print("Press Ctrl-C to stop after the current file is finalized.")
    recorder.run()
    return 0


def _select_device(devices: List[InputDevice]) -> InputDevice:
    _print_devices(devices)
    while True:
        raw_value = input("Choose input device number: ").strip()
        if not raw_value.isdigit():
            print("Enter a device number from the list.")
            continue

        selected_index = int(raw_value)
        for device in devices:
            if device.index == selected_index:
                return device

        print("Selected number is not in the device list.")


def _print_devices(devices: List[InputDevice]) -> None:
    print("Available audio input devices:")
    for device in devices:
        details = device.model_id or "no model id"
        print(f"  {device.index}. {device.name} [{details}] {device.unique_id}")