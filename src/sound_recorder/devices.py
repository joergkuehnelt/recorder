from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from AVFoundation import AVCaptureDevice, AVMediaTypeAudio


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    unique_id: str
    model_id: Optional[str]


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