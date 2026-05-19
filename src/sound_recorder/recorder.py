from __future__ import annotations

import json
import math
import os
import re
import signal
import sys
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
ARMING_DURATION_SECONDS = 3.0
METER_REFRESH_SECONDS = 0.10
METER_FLOOR_DBFS = -60.0
SAFE_TARGET_PEAK_DBFS = -9.0
WARNING_PEAK_DBFS = -3.0
CLIP_PEAK_DBFS = -1.0
WARNING_GAIN_STEP_DB = -4.0
WARNING_GAIN_COOLDOWN_SECONDS = 2.0
MIN_CHANNEL_VOLUME = 0.10
MAX_CHANNEL_VOLUME = 1.00
METER_BAR_WIDTH = 24
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_MAGENTA = "\033[35m"


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
        arming_duration_seconds: float = ARMING_DURATION_SECONDS,
        target_peak_dbfs: float = SAFE_TARGET_PEAK_DBFS,
        warning_peak_dbfs: float = WARNING_PEAK_DBFS,
    ):
        self = objc.super(ChunkedAudioRecorder, self).init()
        if self is None:
            return None

        self.device_id = device_id
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.session_lock_path = self.output_dir / SESSION_LOCK_NAME
        self.segment_seconds = max(1, segment_minutes) * 60
        self.arming_duration_seconds = max(0.1, float(arming_duration_seconds))
        self.target_peak_dbfs = min(-0.1, float(target_peak_dbfs))
        self.warning_peak_dbfs = min(-0.1, float(warning_peak_dbfs))
        self.clip_peak_dbfs = min(-0.1, self.warning_peak_dbfs + 2.0)
        self.session = AVCaptureSession.alloc().init()
        self.audio_output = AVCaptureAudioFileOutput.alloc().init()
        self.capture_device = None
        self.audio_channels: List[object] = []
        self.active_segment: Optional[ActiveSegment] = None
        self.rotation_deadline = 0.0
        self.stop_requested = False
        self.awaiting_finish = False
        self.failure_message: Optional[str] = None
        self.is_configured = False
        self.session_lock_owned = False
        self.current_channel_volume = MAX_CHANNEL_VOLUME
        self.last_meter_render_at = 0.0
        self.last_warning_adjustment_at = 0.0
        self.status_line_width = 0
        self.use_color_output = sys.stdout.isatty()
        return self

    def configure(self) -> None:
        if self.is_configured:
            return

        device = find_input_device(self.device_id)
        self.capture_device = device
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
            self._prepare_audio_monitoring(run_loop)
            self._write_session_lock()
            self._run_arming_step(run_loop)
            if self.stop_requested:
                self._log_line("Recording cancelled during arming.")
                return
            self._start_segment()

            while True:
                run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.10))
                peak_dbfs = self._render_live_meter("REC")
                self._handle_peak_warning(peak_dbfs)
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
                self._log_line(
                    self._decorate_message(
                        "WARN",
                        "Recorder stopped before the current segment finished saving. "
                        "The partial file will be recovered on the next start.",
                        ANSI_YELLOW,
                    )
                )

            if self.session.isRunning():
                self.session.stopRunning()

            self._clear_status_line()
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
        self._log_line(f"Saved {final_path.name}")

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
        self._log_line(f"Recording {temp_path.name}")
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

    def _prepare_audio_monitoring(self, run_loop: NSRunLoop) -> None:
        deadline = time.monotonic() + 2.0

        while time.monotonic() < deadline:
            connections = list(self.audio_output.connections() or [])
            if connections:
                self.audio_channels = list(connections[0].audioChannels() or [])
                if self.audio_channels:
                    break
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.10))

        if not self.audio_channels:
            self._log_line(
                self._decorate_message(
                    "INFO",
                    "Live peak meter unavailable for this device. Recording continues without metering.",
                    ANSI_YELLOW,
                )
            )
            return

        self._apply_channel_volume(MAX_CHANNEL_VOLUME)

    def _run_arming_step(self, run_loop: NSRunLoop) -> None:
        if not self.audio_channels:
            return

        self._log_line(
            self._decorate_message(
                "ARM",
                "Arming input for "
                f"{self.arming_duration_seconds:.1f} seconds. Make the loudest sound you expect to record now.",
                ANSI_CYAN,
            )
        )
        deadline = time.monotonic() + self.arming_duration_seconds
        highest_peak_dbfs = METER_FLOOR_DBFS

        while time.monotonic() < deadline and not self.stop_requested:
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(METER_REFRESH_SECONDS))
            peak_dbfs = self._render_live_meter("ARM")
            if peak_dbfs is not None:
                highest_peak_dbfs = max(highest_peak_dbfs, peak_dbfs)

        self._clear_status_line()
        if self.stop_requested:
            return

        adjusted = self._apply_safe_fixed_gain(highest_peak_dbfs)
        if adjusted:
            self._log_line(
                self._decorate_message(
                    "OK",
                    f"Arming complete. Peak {highest_peak_dbfs:.1f} dBFS. "
                    f"Fixed gain set to {self.current_channel_volume:.2f}.",
                    ANSI_GREEN,
                )
            )
        else:
            self._log_line(
                self._decorate_message(
                    "OK",
                    f"Arming complete. Peak {highest_peak_dbfs:.1f} dBFS. "
                    f"Keeping fixed gain at {self.current_channel_volume:.2f}.",
                    ANSI_GREEN,
                )
            )

    def _apply_safe_fixed_gain(self, peak_dbfs: float) -> bool:
        if peak_dbfs <= self.target_peak_dbfs:
            return False

        delta_db = self.target_peak_dbfs - peak_dbfs
        new_volume = self.current_channel_volume * self._db_to_gain(delta_db)
        return self._apply_channel_volume(new_volume)

    def _handle_peak_warning(self, peak_dbfs: Optional[float]) -> None:
        if peak_dbfs is None or peak_dbfs < self.warning_peak_dbfs:
            return

        now = time.monotonic()
        if now - self.last_warning_adjustment_at < WARNING_GAIN_COOLDOWN_SECONDS:
            return

        self.last_warning_adjustment_at = now
        warning_text = "Peak warning"
        if peak_dbfs >= self.clip_peak_dbfs:
            warning_text = "Clip warning"

        adjusted = self._apply_channel_volume(
            self.current_channel_volume * self._db_to_gain(WARNING_GAIN_STEP_DB)
        )
        if adjusted:
            self._log_line(
                self._decorate_message(
                    "WARN",
                    f"{warning_text}: {peak_dbfs:.1f} dBFS. "
                    f"Reducing fixed gain to {self.current_channel_volume:.2f}.",
                    ANSI_YELLOW if warning_text == "Peak warning" else ANSI_RED,
                )
            )
        else:
            self._log_line(
                self._decorate_message(
                    "WARN",
                    f"{warning_text}: {peak_dbfs:.1f} dBFS. "
                    "Gain is already at the minimum safe setting.",
                    ANSI_YELLOW if warning_text == "Peak warning" else ANSI_RED,
                )
            )

    def _render_live_meter(self, mode: str) -> Optional[float]:
        peak_dbfs = self._read_peak_dbfs()
        if peak_dbfs is None:
            return None

        now = time.monotonic()
        if now - self.last_meter_render_at < METER_REFRESH_SECONDS:
            return peak_dbfs

        clamped_peak = max(METER_FLOOR_DBFS, min(0.0, peak_dbfs))
        normalized = 1.0 - (abs(clamped_peak) / abs(METER_FLOOR_DBFS))
        filled = max(0, min(METER_BAR_WIDTH, int(round(normalized * METER_BAR_WIDTH))))
        bar = self._build_colored_meter_bar(filled)
        warning = ""
        if peak_dbfs >= self.clip_peak_dbfs:
            warning = self._ansi_style(" CLIP", ANSI_RED, bold=True)
        elif peak_dbfs >= self.warning_peak_dbfs:
            warning = self._ansi_style(" HOT", ANSI_YELLOW, bold=True)

        peak_color = ANSI_GREEN
        if peak_dbfs >= self.clip_peak_dbfs:
            peak_color = ANSI_RED
        elif peak_dbfs >= self.warning_peak_dbfs:
            peak_color = ANSI_YELLOW

        mode_label = self._ansi_style(f"{mode:>3}", ANSI_CYAN, bold=True)
        peak_label = self._ansi_style(f"{peak_dbfs:5.1f} dBFS", peak_color, bold=True)
        gain_label = self._ansi_style(f"{self.current_channel_volume:.2f}", ANSI_CYAN)

        status = (
            f"{mode_label} [{bar}] peak {peak_label} "
            f"gain {gain_label}{warning}"
        )
        self._render_status_line(status)
        self.last_meter_render_at = now
        return peak_dbfs

    def _read_peak_dbfs(self) -> Optional[float]:
        if not self.audio_channels:
            return None

        peak_values = []
        for channel in self.audio_channels:
            peak_value = float(channel.peakHoldLevel())
            if not math.isfinite(peak_value):
                continue
            peak_values.append(max(METER_FLOOR_DBFS, min(0.0, peak_value)))

        if not peak_values:
            return None

        return max(peak_values)

    def _apply_channel_volume(self, volume: float) -> bool:
        if not self.audio_channels:
            return False

        clamped_volume = max(MIN_CHANNEL_VOLUME, min(MAX_CHANNEL_VOLUME, volume))
        if abs(clamped_volume - self.current_channel_volume) < 0.01:
            return False

        for channel in self.audio_channels:
            channel.setVolume_(clamped_volume)

        self.current_channel_volume = clamped_volume
        return True

    def _render_status_line(self, message: str) -> None:
        visible_length = len(self._strip_ansi(message))
        padded_message = message + " " * max(0, self.status_line_width - visible_length)
        self.status_line_width = max(self.status_line_width, visible_length)
        print(f"\r{padded_message}", end="", flush=True)

    def _clear_status_line(self) -> None:
        if self.status_line_width == 0:
            return

        print(f"\r{' ' * self.status_line_width}\r", end="", flush=True)
        self.status_line_width = 0

    def _log_line(self, message: str) -> None:
        self._clear_status_line()
        print(message, flush=True)

    def _decorate_message(self, label: str, message: str, color: str) -> str:
        tag = self._ansi_style(f"[{label}]", color, bold=True)
        return f"{tag} {message}"

    def _build_colored_meter_bar(self, filled: int) -> str:
        parts = []
        green_limit = int(round(METER_BAR_WIDTH * 0.60))
        yellow_limit = int(round(METER_BAR_WIDTH * 0.85))

        for index in range(METER_BAR_WIDTH):
            if index < filled:
                color = ANSI_GREEN
                if index >= yellow_limit:
                    color = ANSI_RED
                elif index >= green_limit:
                    color = ANSI_YELLOW
                parts.append(self._ansi_style("#", color, bold=True))
            else:
                parts.append(self._ansi_style("-", ANSI_DIM))

        return "".join(parts)

    def _ansi_style(self, text: str, color: str, bold: bool = False) -> str:
        if not self.use_color_output:
            return text

        prefix = color
        if bold:
            prefix = ANSI_BOLD + color
        return f"{prefix}{text}{ANSI_RESET}"

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return ANSI_ESCAPE_RE.sub("", text)

    @staticmethod
    def _db_to_gain(delta_db: float) -> float:
        return 10 ** (delta_db / 20.0)

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
            self._log_line(
                self._decorate_message(
                    "RECOVER",
                    f"Recovered unfinished recording as {recovered_path.name}",
                    ANSI_MAGENTA,
                )
            )

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

        self._log_line(
            self._decorate_message(
                "TAKEOVER",
                f"Existing recorder session detected for PID {existing_pid}. "
                "Requesting a clean stop before starting a new session.",
                ANSI_YELLOW,
            )
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


def build_recorder(
    device_id: str,
    output_dir: Path,
    segment_minutes: int,
    arming_duration_seconds: float = ARMING_DURATION_SECONDS,
    target_peak_dbfs: float = SAFE_TARGET_PEAK_DBFS,
    warning_peak_dbfs: float = WARNING_PEAK_DBFS,
) -> ChunkedAudioRecorder:
    recorder = ChunkedAudioRecorder.alloc().initWithDeviceID_outputDir_segmentMinutes_(
        device_id,
        str(output_dir),
        segment_minutes,
        arming_duration_seconds,
        target_peak_dbfs,
        warning_peak_dbfs,
    )
    if recorder is None:
        raise RuntimeError("Failed to initialize recorder")
    return recorder