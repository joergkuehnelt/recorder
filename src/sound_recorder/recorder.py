from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import objc
from AVFoundation import (
    AVCaptureAudioFileOutput,
    AVCaptureDeviceInput,
    AVCaptureSession,
    AVFileTypeAppleM4A,
)
from Foundation import NSDate, NSObject, NSRunLoop, NSURL

from sound_recorder.devices import find_input_device


PARTIAL_FILE_SUFFIX = ".partial.m4a"
SESSION_LOCK_NAME = ".recording-session.json"
PARTIAL_FILE_PATTERN = re.compile(r"^\.(\d{8})-(\d{6})\.partial\.m4a$")


def _format_output_name(started_at: datetime, ended_at: datetime) -> str:
    return (
        f"{started_at:%d%m%Y}-start{started_at:%H%M}-end{ended_at:%H%M}.m4a"
    )


def _temp_output_name(started_at: datetime) -> str:
    return f".{started_at:%Y%m%d-%H%M%S}.partial.m4a"


def _parse_partial_started_at(path: Path) -> Optional[datetime]:
    match = PARTIAL_FILE_PATTERN.match(path.name)
    if match is None:
        return None

    return datetime.strptime("-".join(match.groups()), "%Y%m%d-%H%M%S")


@dataclass
class ActiveSegment:
    started_at: datetime
    temp_path: Path


class ChunkedAudioRecorder(NSObject):
    def initWithDeviceID_outputDir_segmentMinutes_(
        self,
        device_id: str,
        output_dir: str,
        segment_minutes: int,
    ):
        self = objc.super(ChunkedAudioRecorder, self).init()
        if self is None:
            return None

        self.device_id = device_id
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session_lock_path = self.output_dir / SESSION_LOCK_NAME
        self.segment_seconds = max(1, segment_minutes) * 60
        self.session = AVCaptureSession.alloc().init()
        self.audio_output = AVCaptureAudioFileOutput.alloc().init()
        self.active_segment: Optional[ActiveSegment] = None
        self.rotation_deadline = 0.0
        self.stop_requested = False
        self.awaiting_finish = False
        self.failure_message: Optional[str] = None
        self.is_configured = False
        self.session_lock_owned = False
        return self

    def configure(self) -> None:
        if self.is_configured:
            return

        device = find_input_device(self.device_id)
        device_input, error = AVCaptureDeviceInput.deviceInputWithDevice_error_(
            device,
            None,
        )
        if device_input is None:
            raise RuntimeError(self._describe_error(error, "Failed to create device input"))

        if not self.session.canAddInput_(device_input):
            raise RuntimeError("AVCaptureSession cannot add the selected input device")
        self.session.addInput_(device_input)

        if not self.session.canAddOutput_(self.audio_output):
            raise RuntimeError("AVCaptureSession cannot add audio file output")
        self.session.addOutput_(self.audio_output)
        self.is_configured = True

    def run(self) -> None:
        run_loop = NSRunLoop.currentRunLoop()

        try:
            self._take_over_existing_session()
            self._recover_stale_partials()
            self.configure()
            self.session.startRunning()
            self._install_signal_handlers()
            self._write_session_lock()
            self._start_segment()

            while True:
                run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.25))
                self._rotate_if_due()

                if self.failure_message:
                    raise RuntimeError(self.failure_message)

                if self.stop_requested and not self.audio_output.isRecording():
                    break
        finally:
            if self.audio_output.isRecording():
                self.audio_output.stopRecording()

            finished_cleanly = self._wait_for_recording_stop(run_loop, timeout_seconds=10.0)
            if not finished_cleanly and self.active_segment is not None:
                print(
                    "Recorder stopped before the current segment finished saving. "
                    "The partial file will be recovered on the next start."
                )

            if self.session.isRunning():
                self.session.stopRunning()

            self._release_session_lock()

    def request_stop(self) -> None:
        self.stop_requested = True
        if self.audio_output.isRecording() and not self.awaiting_finish:
            self.awaiting_finish = True
            self.audio_output.stopRecording()

    def captureOutput_didFinishRecordingToOutputFileAtURL_fromConnections_error_(
        self,
        capture_output,
        output_file_url,
        connections,
        error,
    ):
        del capture_output, connections

        segment = self.active_segment
        self.active_segment = None
        self.awaiting_finish = False
        ended_at = datetime.now()
        self._write_session_lock()

        if error is not None:
            self.failure_message = self._describe_error(error, "Recording failed")
            return

        if segment is None:
            self.failure_message = "Recording finished without an active segment"
            return

        recorded_path = Path(str(output_file_url.path()))
        final_path = self.output_dir / _format_output_name(segment.started_at, ended_at)
        final_path = self._deduplicate(final_path)
        recorded_path.replace(final_path)
        print(f"Saved {final_path.name}")

        if not self.stop_requested:
            self._start_segment()

    def _start_segment(self) -> None:
        if self.active_segment is not None or self.audio_output.isRecording():
            raise RuntimeError(
                "Cannot start a new segment before the previous one is finalized"
            )

        started_at = datetime.now()
        temp_path = self.output_dir / _temp_output_name(started_at)
        self.active_segment = ActiveSegment(started_at=started_at, temp_path=temp_path)
        self.rotation_deadline = time.monotonic() + self.segment_seconds
        self.awaiting_finish = False
        self._write_session_lock(temp_path)

        temp_url = NSURL.fileURLWithPath_(str(temp_path))
        print(f"Recording {temp_path.name}")
        self.audio_output.startRecordingToOutputFileURL_outputFileType_recordingDelegate_(
            temp_url,
            AVFileTypeAppleM4A,
            self,
        )

    def _rotate_if_due(self) -> None:
        if self.stop_requested or self.awaiting_finish or self.active_segment is None:
            return

        if time.monotonic() >= self.rotation_deadline and self.audio_output.isRecording():
            self.awaiting_finish = True
            self.audio_output.stopRecording()

    def _install_signal_handlers(self) -> None:
        def _handle_stop(signum, frame):
            del signum, frame
            self.request_stop()

        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)

    @staticmethod
    def _describe_error(error, fallback: str) -> str:
        if error is None:
            return fallback

        description = getattr(error, "localizedDescription", None)
        if callable(description):
            return f"{fallback}: {description()}"

        return f"{fallback}: {error}"

    def _recover_stale_partials(self) -> None:
        for candidate in sorted(self.output_dir.iterdir()):
            if not candidate.is_file() or not candidate.name.endswith(PARTIAL_FILE_SUFFIX):
                continue

            stats = candidate.stat()
            started_at = _parse_partial_started_at(candidate)
            if started_at is None:
                started_at = datetime.fromtimestamp(stats.st_mtime)

            ended_timestamp = max(stats.st_mtime, started_at.timestamp())
            ended_at = datetime.fromtimestamp(ended_timestamp)
            recovered_path = self._deduplicate(
                self.output_dir / _format_output_name(started_at, ended_at)
            )
            candidate.replace(recovered_path)
            print(f"Recovered unfinished recording as {recovered_path.name}")

    def _take_over_existing_session(self) -> None:
        session_info = self._load_session_lock()
        if session_info is None:
            return

        existing_pid = session_info.get("pid")
        if not isinstance(existing_pid, int) or existing_pid == os.getpid():
            self._clear_stale_session_lock()
            return

        if not self._is_process_running(existing_pid):
            self._clear_stale_session_lock()
            return

        if not self._looks_like_recorder_process(existing_pid):
            raise RuntimeError(
                "Another process already owns the recorder session lock. "
                "Stop it manually before starting a new session."
            )

        print(
            f"Existing recorder session detected for PID {existing_pid}. "
            "Requesting a clean stop before starting a new session."
        )
        os.kill(existing_pid, signal.SIGINT)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not self.session_lock_path.exists() or not self._is_process_running(existing_pid):
                break
            time.sleep(0.25)

        if self._is_process_running(existing_pid):
            raise RuntimeError(
                "Existing recorder session did not stop cleanly. "
                "Refusing to start a second session."
            )

        self._clear_stale_session_lock()

    def _wait_for_recording_stop(self, run_loop: NSRunLoop, timeout_seconds: float) -> bool:
        timeout_at = time.monotonic() + timeout_seconds
        while self.audio_output.isRecording() and time.monotonic() < timeout_at:
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))

        return not self.audio_output.isRecording()

    def _load_session_lock(self) -> Optional[Dict[str, Any]]:
        if not self.session_lock_path.exists():
            return None

        try:
            return json.loads(self.session_lock_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _write_session_lock(self, temp_path: Optional[Path] = None) -> None:
        payload = {
            "pid": os.getpid(),
            "device_id": self.device_id,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "active_temp_file": str(temp_path) if temp_path is not None else None,
        }
        self.session_lock_path.write_text(json.dumps(payload, indent=2))
        self.session_lock_owned = True

    def _release_session_lock(self) -> None:
        if not self.session_lock_owned:
            return

        try:
            self.session_lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.session_lock_owned = False

    def _clear_stale_session_lock(self) -> None:
        try:
            self.session_lock_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _is_process_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False

        return True

    @staticmethod
    def _looks_like_recorder_process(pid: int) -> bool:
        try:
            output = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return False

        return "sound_recorder" in output or "sound-recorder" in output

    @staticmethod
    def _deduplicate(path: Path) -> Path:
        if not path.exists():
            return path

        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1

        while True:
            candidate = parent / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1


def build_recorder(device_id: str, output_dir: Path, segment_minutes: int) -> ChunkedAudioRecorder:
    recorder = ChunkedAudioRecorder.alloc().initWithDeviceID_outputDir_segmentMinutes_(
        device_id,
        str(output_dir),
        segment_minutes,
    )
    if recorder is None:
        raise RuntimeError("Failed to initialize recorder")
    return recorder