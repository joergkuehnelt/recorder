from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import shutil
import sys
import subprocess
import termios
import time
import tty
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from sound_recorder.playlist import (
    LOCAL_STATE_PATH,
    get_remembered_song_history_path,
    read_last_state_entry,
    read_song_history_entries,
)


PARTIAL_FILE_SUFFIX = ".partial.m4a"
SESSION_LOCK_NAME = ".recording-session.json"
PARTIAL_FILE_PATTERN = re.compile(r"^\.(\d{8})-(\d{6})\.partial\.m4a$")
ARMING_DURATION_SECONDS = 3.0
METER_REFRESH_SECONDS = 0.03
METER_FLOOR_DBFS = -60.0
SAFE_TARGET_PEAK_DBFS = -9.0
WARNING_PEAK_DBFS = -3.0
CLIP_PEAK_DBFS = -1.0
WARNING_GAIN_STEP_DB = -4.0
WARNING_GAIN_COOLDOWN_SECONDS = 2.0
MIN_CHANNEL_VOLUME = 0.10
MAX_CHANNEL_VOLUME = 1.00
METER_BAR_WIDTH = 36
PEAK_HOLD_DECAY_DB_PER_SECOND = 14.0
LAST_STATE_REFRESH_SECONDS = 1.0
SONG_HISTORY_MATCH_WINDOW_SECONDS = 90.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_MAGENTA = "\033[35m"
ANSI_ORANGE = "\033[38;5;208m"
CAFFEINATE_PATH = "/usr/bin/caffeinate"

LEVEL_METER_ASCII_ART = (
    "_      _              _             _     _         _                                      _",
    "| |    (_)            | |           | |   (_)       ( )                                    | |",
    "| |__   _  _ __   ___ | |__    __ _ | | __ _  _ __  |/   _ __   ___   ___   ___   _ __   __| |  ___  _ __",
    "| '_ \\ | || '_ \\ / __|| '_ \\  / _` || |/ /| || '_ \\     | '__| / _ \\ / __| / _ \\ | '__| / _` | / _ \\| '__|",
    "| | | || || |_) |\\__ \\| | | || (_| ||   < | || | | |    | |   |  __/| (__ | (_) || |   | (_| ||  __/| |",
    "|_| |_||_|| .__/ |___/|_| |_| \\__,_||_|\\_\\|_||_| |_|    |_|    \\___| \\___| \\___/ |_|    \\__,_| \\___||_|",
    "          | |",
    "          |_|",
)


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


@dataclass
class SegmentTrackEvent:
    observed_at: datetime
    display_text: str
    source_path: Optional[str]


@dataclass
class CombinedTrackEvent:
    observed_at: datetime
    display_text: str
    artist: str
    title: str
    source: str
    source_path: Optional[str]
    raw_line: Optional[str] = None


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
        self.restart_requested = False
        self.awaiting_finish = False
        self.failure_message: Optional[str] = None
        self.is_configured = False
        self.session_lock_owned = False
        self.current_channel_volume = MAX_CHANNEL_VOLUME
        self.last_meter_render_at = 0.0
        self.last_warning_adjustment_at = 0.0
        self.peak_hold_dbfs = METER_FLOOR_DBFS
        self.last_peak_sample_at = time.monotonic()
        self.last_state_display = "NO DETECTION"
        self.last_state_refresh_at = 0.0
        self.last_state_source_path: Optional[str] = None
        self.segment_track_events: List[SegmentTrackEvent] = []
        self.meter_header_rendered = False
        self.status_line_widths: List[int] = []
        self.use_color_output = sys.stdout.isatty()
        self.command_input_enabled = sys.stdin.isatty()
        self.stdin_fd: Optional[int] = None
        self.stdin_termios_state = None
        self.caffeinate_process: Optional[subprocess.Popen[str]] = None
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
            self._start_caffeinate()
            self.session.startRunning()
            self._install_signal_handlers()
            self._enable_runtime_command_input()
            self._prepare_audio_monitoring(run_loop)
            self._write_session_lock()
            self._run_arming_step(run_loop)
            if self.stop_requested:
                self._log_line("Recording cancelled during arming.")
                return
            self._start_segment()

            while True:
                run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(METER_REFRESH_SECONDS))
                self._poll_runtime_command()
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

            self._disable_runtime_command_input()
            self._clear_status_line()
            self._release_session_lock()
            self._stop_caffeinate()

    def request_stop(self) -> None:
        self.stop_requested = True
        if self.audio_output.isRecording() and not self.awaiting_finish:
            self.awaiting_finish = True
            self.audio_output.stopRecording()

    def request_restart(self) -> None:
        if not self.audio_output.isRecording() or self.awaiting_finish:
            return

        self.restart_requested = True
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
        should_restart = self.restart_requested and not self.stop_requested
        self.restart_requested = False

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
        self._write_segment_sidecar(final_path, segment.started_at, ended_at)
        segment_length = self._format_elapsed_seconds(
            max(0, int((ended_at - segment.started_at).total_seconds()))
        )
        self._log_line(f"Saved {final_path.name} ({segment_length})")

        if should_restart:
            self._log_line(
                self._decorate_message(
                    "RESTART",
                    "Starting a new recording segment on user request.",
                    ANSI_CYAN,
                )
            )
            self._start_segment()
        elif not self.stop_requested:
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
        self.peak_hold_dbfs = METER_FLOOR_DBFS
        self.last_peak_sample_at = time.monotonic()
        self.segment_track_events = []
        self.last_state_refresh_at = 0.0
        self.last_state_display = self._load_last_state_display()
        self._capture_segment_track_event(started_at, force=True)
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

    def _enable_runtime_command_input(self) -> None:
        if not self.command_input_enabled:
            return

        try:
            self.stdin_fd = sys.stdin.fileno()
            self.stdin_termios_state = termios.tcgetattr(self.stdin_fd)
            tty.setcbreak(self.stdin_fd)
        except (termios.error, ValueError, OSError):
            self.command_input_enabled = False
            self.stdin_fd = None
            self.stdin_termios_state = None

    def _disable_runtime_command_input(self) -> None:
        if not self.command_input_enabled or self.stdin_fd is None or self.stdin_termios_state is None:
            return

        try:
            termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.stdin_termios_state)
        except (termios.error, ValueError, OSError):
            pass
        finally:
            self.stdin_fd = None
            self.stdin_termios_state = None

    def _start_caffeinate(self) -> None:
        if self.caffeinate_process is not None:
            return

        if not Path(CAFFEINATE_PATH).exists():
            self._log_line(
                self._decorate_message(
                    "INFO",
                    "Keep-awake helper unavailable on this system. Recording continues without caffeinate.",
                    ANSI_YELLOW,
                )
            )
            return

        try:
            self.caffeinate_process = subprocess.Popen(
                [CAFFEINATE_PATH, "-dimsu", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            self.caffeinate_process = None
            self._log_line(
                self._decorate_message(
                    "WARN",
                    "Keep-awake helper could not be started. Recording continues without caffeinate.",
                    ANSI_YELLOW,
                )
            )
            return

        self._log_line(
            self._decorate_message(
                "INFO",
                "Keep-awake active. Preventing sleep and screensaver during recording.",
                ANSI_CYAN,
            )
        )

    def _stop_caffeinate(self) -> None:
        if self.caffeinate_process is None:
            return

        process = self.caffeinate_process
        self.caffeinate_process = None
        if process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()

    def _poll_runtime_command(self) -> None:
        if not self.command_input_enabled or self.stdin_fd is None:
            return

        try:
            readable, _, _ = select.select([self.stdin_fd], [], [], 0)
        except (OSError, ValueError):
            return

        if not readable:
            return

        try:
            command = os.read(self.stdin_fd, 1).decode("utf-8", errors="ignore").lower()
        except OSError:
            return

        if command == "s":
            self._log_line(
                self._decorate_message(
                    "STOP",
                    "Stop requested. Saving the current file before exit.",
                    ANSI_YELLOW,
                )
            )
            self.request_stop()
        elif command == "r":
            self._log_line(
                self._decorate_message(
                    "RESTART",
                    "Restart requested. Finalizing the current file and starting a new one.",
                    ANSI_CYAN,
                )
            )
            self.request_restart()

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
        hold_dbfs = self._update_peak_hold(clamped_peak)
        hold_normalized = 1.0 - (abs(hold_dbfs) / abs(METER_FLOOR_DBFS))
        hold_index = max(0, min(METER_BAR_WIDTH - 1, int(round(hold_normalized * (METER_BAR_WIDTH - 1)))))
        bar = self._build_colored_meter_bar(filled, hold_index)
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
        hold_label = self._ansi_style(f"hold {hold_dbfs:5.1f}", ANSI_DIM)
        gain_label = self._ansi_style(f"{self.current_channel_volume:.2f}", ANSI_CYAN)
        elapsed_label = self._ansi_style(self._format_current_segment_elapsed(), ANSI_ORANGE, bold=True)

        self._render_meter_header_once(mode)
        status = (
            f"{mode_label} <{bar}> len {elapsed_label} peak {peak_label} {hold_label} "
            f"gain {gain_label}{warning}  "
            f"{self._ansi_style('[S] stop', ANSI_YELLOW)} "
            f"{self._ansi_style('[R] restart', ANSI_CYAN)}"
        )
        if mode == "REC":
            state_line = self._ansi_style(
                f"[NOW] {self._current_last_state_text()}",
                ANSI_GREEN,
                bold=True,
            )
            self._render_status_block([status, state_line])
        else:
            self._render_status_block([status])
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

    def _render_status_block(self, lines: List[str]) -> None:
        fitted_lines = [self._fit_status_message(line) for line in lines]
        previous_widths = self.status_line_widths[:]
        width_count = max(len(previous_widths), len(fitted_lines))
        new_widths: List[int] = []

        print("\r", end="", flush=True)
        for index in range(width_count):
            line = fitted_lines[index] if index < len(fitted_lines) else ""
            visible_length = len(self._strip_ansi(line))
            previous_width = previous_widths[index] if index < len(previous_widths) else 0
            padded_line = line + " " * max(0, previous_width - visible_length)
            print(padded_line, end="", flush=True)
            new_widths.append(max(previous_width, visible_length))
            if index < width_count - 1:
                print("\n", end="", flush=True)

        move_up = max(0, width_count - 1)
        if move_up:
            print(f"\033[{move_up}A", end="", flush=True)

        self.status_line_widths = new_widths[: len(fitted_lines)]

    def _clear_status_line(self) -> None:
        if not self.status_line_widths:
            return

        line_count = len(self.status_line_widths)
        print("\r", end="", flush=True)
        for index, width in enumerate(self.status_line_widths):
            print(" " * width, end="", flush=True)
            if index < line_count - 1:
                print("\n", end="", flush=True)
        if line_count > 1:
            print(f"\033[{line_count - 1}A", end="", flush=True)
        print("\r", end="", flush=True)
        self.status_line_widths = []

    def _log_line(self, message: str) -> None:
        self._clear_status_line()
        print(message, flush=True)

    def _render_meter_header_once(self, mode: str) -> None:
        if mode != "REC" or self.meter_header_rendered:
            return

        for line in LEVEL_METER_ASCII_ART:
            print(self._ansi_style(line.rstrip(), ANSI_ORANGE, bold=True), flush=True)

        self.meter_header_rendered = True

    def _current_last_state_text(self) -> str:
        now = time.monotonic()
        if now - self.last_state_refresh_at < LAST_STATE_REFRESH_SECONDS:
            return self.last_state_display

        self.last_state_refresh_at = now
        self.last_state_display = self._load_last_state_display()
        if self.active_segment is not None:
            self._capture_segment_track_event(datetime.now())
        return self.last_state_display

    def _format_current_segment_elapsed(self) -> str:
        if self.active_segment is None:
            return "00:00"

        elapsed_seconds = max(
            0,
            int((datetime.now() - self.active_segment.started_at).total_seconds()),
        )
        return self._format_elapsed_seconds(elapsed_seconds)

    @staticmethod
    def _format_elapsed_seconds(elapsed_seconds: int) -> str:
        minutes, seconds = divmod(elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _load_last_state_display(self) -> str:
        try:
            payload = json.loads(LOCAL_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "NO DETECTION"

        if not isinstance(payload, dict):
            return "NO DETECTION"

        raw_path = payload.get("last_state_json_path")
        if not isinstance(raw_path, str) or not raw_path:
            self.last_state_source_path = None
            return "NO DETECTION"

        last_state_path = Path(raw_path).expanduser()
        resolved_path = last_state_path.resolve() if last_state_path.exists() else None
        self.last_state_source_path = str(resolved_path) if resolved_path is not None else None
        entry = read_last_state_entry(resolved_path)
        return entry or "NO DETECTION"

    def _capture_segment_track_event(self, observed_at: datetime, force: bool = False) -> None:
        if self.active_segment is None:
            return

        display_text = self.last_state_display or "NO DETECTION"
        if not force and self.segment_track_events:
            if self.segment_track_events[-1].display_text == display_text:
                return

        self.segment_track_events.append(
            SegmentTrackEvent(
                observed_at=observed_at,
                display_text=display_text,
                source_path=self.last_state_source_path,
            )
        )

    def _write_segment_sidecar(
        self,
        audio_path: Path,
        segment_started_at: datetime,
        segment_ended_at: datetime,
    ) -> None:
        sidecar_path = audio_path.with_suffix(".cuesheet.json")
        track_events = self._collect_combined_track_events(segment_started_at, segment_ended_at)
        if not track_events:
            artist, title = self._parse_track_display(self.last_state_display or "NO DETECTION")
            track_events = [
                CombinedTrackEvent(
                    observed_at=segment_started_at,
                    display_text=self.last_state_display or "NO DETECTION",
                    artist=artist,
                    title=title,
                    source="last_state",
                    source_path=self.last_state_source_path,
                )
            ]
        payload = {
            "audio_file": audio_path.name,
            "segment_started_at": segment_started_at.isoformat(timespec="seconds"),
            "segment_ended_at": segment_ended_at.isoformat(timespec="seconds"),
            "segment_duration_seconds": max(
                0.0,
                round((segment_ended_at - segment_started_at).total_seconds(), 3),
            ),
            "tracks": [
                self._build_sidecar_track_payload(
                    event,
                    segment_started_at,
                    segment_ended_at,
                    index,
                    len(track_events),
                )
                for index, event in enumerate(track_events, start=1)
            ],
        }
        sidecar_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._write_segment_cue_sheet(audio_path, payload)

    def _collect_combined_track_events(
        self,
        segment_started_at: datetime,
        segment_ended_at: datetime,
    ) -> List[CombinedTrackEvent]:
        combined: List[CombinedTrackEvent] = []
        for event in self.segment_track_events:
            artist, title = self._parse_track_display(event.display_text)
            combined.append(
                CombinedTrackEvent(
                    observed_at=event.observed_at,
                    display_text=event.display_text,
                    artist=artist,
                    title=title,
                    source="last_state",
                    source_path=event.source_path,
                )
            )

        song_history_path = get_remembered_song_history_path()
        window_start = segment_started_at - timedelta(seconds=60)
        window_end = segment_ended_at + timedelta(seconds=60)
        for entry in read_song_history_entries(song_history_path):
            if entry.observed_at < window_start or entry.observed_at > window_end:
                continue
            combined.append(
                CombinedTrackEvent(
                    observed_at=entry.observed_at,
                    display_text=entry.display_text,
                    artist=entry.artist,
                    title=entry.title,
                    source="song_history",
                    source_path=str(song_history_path) if song_history_path is not None else None,
                    raw_line=entry.raw_line,
                )
            )

        return self._merge_track_events(combined)

    def _merge_track_events(self, events: List[CombinedTrackEvent]) -> List[CombinedTrackEvent]:
        merged: List[CombinedTrackEvent] = []
        source_priority = {"last_state": 0, "song_history": 1}
        for event in sorted(events, key=lambda item: (item.observed_at, source_priority.get(item.source, 9))):
            if not merged:
                merged.append(event)
                continue

            previous = merged[-1]
            same_track = previous.artist == event.artist and previous.title == event.title
            close_in_time = abs((event.observed_at - previous.observed_at).total_seconds()) <= SONG_HISTORY_MATCH_WINDOW_SECONDS
            if same_track and close_in_time:
                earliest_event = previous if previous.observed_at <= event.observed_at else event
                preferred_source = previous if previous.source == "last_state" else event
                merged[-1] = CombinedTrackEvent(
                    observed_at=earliest_event.observed_at,
                    display_text=preferred_source.display_text,
                    artist=preferred_source.artist,
                    title=preferred_source.title,
                    source=preferred_source.source,
                    source_path=preferred_source.source_path,
                    raw_line=preferred_source.raw_line,
                )
                continue

            merged.append(event)

        return merged

    def _build_sidecar_track_payload(
        self,
        event: CombinedTrackEvent,
        segment_started_at: datetime,
        segment_ended_at: datetime,
        index: int,
        total_tracks: int,
    ) -> Dict[str, Any]:
        offset_seconds = max(
            0.0,
            round((event.observed_at - segment_started_at).total_seconds(), 3),
        )
        if event.observed_at > segment_ended_at:
            offset_seconds = max(
                0.0,
                round((segment_ended_at - segment_started_at).total_seconds(), 3),
            )

        return {
            "track_number": index,
            "offset_seconds": offset_seconds,
            "observed_at": event.observed_at.isoformat(timespec="seconds"),
            "display": event.display_text,
            "artist": event.artist,
            "title": event.title,
            "source": event.source,
            "source_path": event.source_path,
            "raw_line": event.raw_line,
            "partial_at_start": index == 1,
            "partial_at_end": index == total_tracks,
        }

    def _write_segment_cue_sheet(self, audio_path: Path, payload: Dict[str, Any]) -> None:
        cue_path = audio_path.with_suffix(".cue")
        lines = [
            f'FILE "{self._escape_cue_text(audio_path.name)}" MP4',
        ]

        tracks = payload.get("tracks", [])
        if not isinstance(tracks, list):
            tracks = []

        for track in tracks:
            if not isinstance(track, dict):
                continue

            track_number = int(track.get("track_number", 1))
            title = str(track.get("title") or "NO DETECTION")
            artist = str(track.get("artist") or "UNKNOWN")
            offset_seconds = float(track.get("offset_seconds", 0.0))

            lines.extend(
                [
                    f"  TRACK {track_number:02d} AUDIO",
                    f'    TITLE "{self._escape_cue_text(title)}"',
                    f'    PERFORMER "{self._escape_cue_text(artist)}"',
                    f"    INDEX 01 {self._format_cue_index(offset_seconds)}",
                ]
            )

        cue_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _parse_track_display(display_text: str) -> tuple[str, str]:
        parts = [part.strip() for part in display_text.split("=>")]
        if len(parts) >= 3:
            return parts[1] or "UNKNOWN", parts[2] or "NO DETECTION"
        if len(parts) == 2 and parts[1].upper() == "NO DETECTION":
            return "UNKNOWN", "NO DETECTION"
        cleaned = display_text.strip()
        if not cleaned:
            return "UNKNOWN", "NO DETECTION"
        return "UNKNOWN", cleaned

    @staticmethod
    def _format_cue_index(offset_seconds: float) -> str:
        total_frames = max(0, int(round(offset_seconds * 75)))
        minutes, remaining_frames = divmod(total_frames, 75 * 60)
        seconds, frames = divmod(remaining_frames, 75)
        return f"{minutes:02d}:{seconds:02d}:{frames:02d}"

    @staticmethod
    def _escape_cue_text(text: str) -> str:
        return text.replace('"', "'")

    def _fit_status_message(self, message: str) -> str:
        terminal_width = max(20, shutil.get_terminal_size((160, 24)).columns - 1)
        visible_message = self._strip_ansi(message)
        if len(visible_message) <= terminal_width:
            return message

        suffix = self._current_last_state_text()
        state_label = self._ansi_style(suffix, ANSI_GREEN, bold=True)
        if state_label in message:
            prefix_part, _, _ = message.partition(state_label)
            visible_state = self._strip_ansi(state_label)
            prefix_limit = max(0, terminal_width - len(visible_state) - 2)
            if prefix_limit > 0:
                truncated_prefix = self._truncate_plain_text(
                    self._strip_ansi(prefix_part).rstrip(),
                    prefix_limit,
                )
                candidate = f"{truncated_prefix}  {state_label}" if truncated_prefix else state_label
                if len(self._strip_ansi(candidate)) <= terminal_width:
                    return candidate

            if len(visible_state) > terminal_width:
                return self._ansi_style(
                    self._truncate_plain_text(visible_state, terminal_width),
                    ANSI_GREEN,
                    bold=True,
                )
            return state_label

        return self._truncate_plain_text(visible_message, terminal_width)

    @staticmethod
    def _truncate_plain_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[: limit - 3].rstrip() + "..."

    def _decorate_message(self, label: str, message: str, color: str) -> str:
        tag = self._ansi_style(f"[{label}]", color, bold=True)
        return f"{tag} {message}"

    def _build_colored_meter_bar(self, filled: int, hold_index: int) -> str:
        parts = []
        green_limit = int(round(METER_BAR_WIDTH * 0.60))
        yellow_limit = int(round(METER_BAR_WIDTH * 0.85))

        for index in range(METER_BAR_WIDTH):
            if index == hold_index:
                marker_color = ANSI_CYAN
                if index >= yellow_limit:
                    marker_color = ANSI_RED
                elif index >= green_limit:
                    marker_color = ANSI_YELLOW
                elif index < filled:
                    marker_color = ANSI_GREEN
                parts.append(self._ansi_style("|", marker_color, bold=True))
            elif index < filled:
                color = ANSI_GREEN
                if index >= yellow_limit:
                    color = ANSI_RED
                elif index >= green_limit:
                    color = ANSI_YELLOW
                fill_char = "="
                if index >= yellow_limit:
                    fill_char = "^"
                elif index >= green_limit:
                    fill_char = "!"
                parts.append(self._ansi_style(fill_char, color, bold=True))
            else:
                parts.append(self._ansi_style("-", ANSI_DIM))

        return "".join(parts)

    def _update_peak_hold(self, current_peak_dbfs: float) -> float:
        now = time.monotonic()
        elapsed = max(0.0, now - self.last_peak_sample_at)
        self.last_peak_sample_at = now

        decayed_hold = self.peak_hold_dbfs - (PEAK_HOLD_DECAY_DB_PER_SECOND * elapsed)
        self.peak_hold_dbfs = max(METER_FLOOR_DBFS, max(current_peak_dbfs, decayed_hold))
        return self.peak_hold_dbfs

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