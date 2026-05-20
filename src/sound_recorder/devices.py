from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Dict, List, Optional

from AVFoundation import (
    AVCaptureAudioDataOutput,
    AVCaptureDevice,
    AVCaptureDeviceInput,
    AVCaptureSession,
    AVMediaTypeAudio,
)
from Foundation import NSDate, NSRunLoop


METER_FLOOR_DBFS = -60.0


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    unique_id: str
    model_id: Optional[str]


@dataclass
class _InputLevelMonitorEntry:
    device_id: str
    session: object
    output: object
    channels: List[object]
    last_peak_dbfs: Optional[float] = None


def list_input_devices() -> List[InputDevice]:
    devices = AVCaptureDevice.devicesWithMediaType_(AVMediaTypeAudio) or []
    result: List[InputDevice] = []

    for index, device in enumerate(devices, start=1):
        model_id = device.modelID() if hasattr(device, "modelID") else None
        result.append(
            InputDevice(
                index=index,
                name=str(device.localizedName()),
                unique_id=str(device.uniqueID()),
                model_id=str(model_id) if model_id else None,
            )
        )

    return result


def find_input_device(unique_id: str) -> AVCaptureDevice:
    devices = AVCaptureDevice.devicesWithMediaType_(AVMediaTypeAudio) or []
    for device in devices:
        if str(device.uniqueID()) == unique_id:
            return device

    raise ValueError(f"No input device found for unique id: {unique_id}")


class InputLevelMonitor:
    def __init__(self, device_ids: List[str]) -> None:
        self.device_ids = list(device_ids)
        self.entries: List[_InputLevelMonitorEntry] = []
        self.started = False

    def __enter__(self) -> InputLevelMonitor:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.stop()

    def start(self) -> None:
        if self.started:
            return

        for device_id in self.device_ids:
            try:
                device = find_input_device(device_id)
            except ValueError:
                continue

            session = AVCaptureSession.alloc().init()
            output = AVCaptureAudioDataOutput.alloc().init()
            device_input, error = AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
            if device_input is None:
                del error
                continue

            if not session.canAddInput_(device_input) or not session.canAddOutput_(output):
                continue

            session.addInput_(device_input)
            session.addOutput_(output)
            session.startRunning()
            self.entries.append(
                _InputLevelMonitorEntry(
                    device_id=device_id,
                    session=session,
                    output=output,
                    channels=[],
                )
            )

        self.started = True
        self._prime_channels()

    def stop(self) -> None:
        for entry in self.entries:
            if entry.session.isRunning():
                entry.session.stopRunning()
        self.entries = []
        self.started = False

    def poll_levels(self) -> Dict[str, Optional[float]]:
        if not self.started:
            return {device_id: None for device_id in self.device_ids}

        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.001))
        levels: Dict[str, Optional[float]] = {device_id: None for device_id in self.device_ids}

        for entry in self.entries:
            if not entry.channels:
                self._refresh_channels(entry)
            if not entry.channels:
                levels[entry.device_id] = entry.last_peak_dbfs
                continue

            peak_values: List[float] = []
            for channel in entry.channels:
                peak_value = float(channel.peakHoldLevel())
                if not math.isfinite(peak_value):
                    continue
                peak_values.append(max(METER_FLOOR_DBFS, min(0.0, peak_value)))

            if peak_values:
                entry.last_peak_dbfs = max(peak_values)
            levels[entry.device_id] = entry.last_peak_dbfs

        return levels

    def _prime_channels(self) -> None:
        deadline = time.monotonic() + 1.0
        unresolved = True
        while unresolved and time.monotonic() < deadline:
            unresolved = False
            for entry in self.entries:
                unresolved = self._refresh_channels(entry) or unresolved
            if unresolved:
                NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))

    def _refresh_channels(self, entry: _InputLevelMonitorEntry) -> bool:
        connections = list(entry.output.connections() or [])
        if not connections:
            return True

        entry.channels = list(connections[0].audioChannels() or [])
        return not entry.channels