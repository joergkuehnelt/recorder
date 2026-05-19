"""Tests for the sound_recorder.recorder module."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import regression guard – the original bug was an objc.BadPrototypeError
# raised at import time when a method name collided with an NSObject selector.
# ---------------------------------------------------------------------------

def test_import_recorder_module():
    """Importing recorder must not raise objc.BadPrototypeError."""
    from sound_recorder import recorder  # noqa: F401


def test_import_chunked_audio_recorder_class():
    from sound_recorder.recorder import ChunkedAudioRecorder  # noqa: F401


# ---------------------------------------------------------------------------
# Module-level helper functions
# ---------------------------------------------------------------------------

class TestFormatOutputName:
    def test_basic(self):
        from sound_recorder.recorder import _format_output_name

        started = datetime(2025, 3, 15, 9, 30, 0)
        ended = datetime(2025, 3, 15, 10, 30, 0)
        assert _format_output_name(started, ended) == "15032025-start0930-end1030.m4a"

    def test_midnight_boundary(self):
        from sound_recorder.recorder import _format_output_name

        started = datetime(2025, 12, 31, 23, 45, 0)
        ended = datetime(2026, 1, 1, 0, 15, 0)
        name = _format_output_name(started, ended)
        assert name.startswith("31122025-start2345-")
        assert name.endswith(".m4a")


class TestTempOutputName:
    def test_format(self):
        from sound_recorder.recorder import _temp_output_name

        dt = datetime(2025, 7, 4, 14, 5, 33)
        assert _temp_output_name(dt) == ".20250704-140533.partial.m4a"

    def test_hidden_file(self):
        from sound_recorder.recorder import _temp_output_name

        name = _temp_output_name(datetime(2025, 1, 1, 0, 0, 0))
        assert name.startswith(".")


class TestParsePartialStartedAt:
    def test_valid_name(self):
        from sound_recorder.recorder import _parse_partial_started_at

        path = Path(".20250315-093000.partial.m4a")
        result = _parse_partial_started_at(path)
        assert result == datetime(2025, 3, 15, 9, 30, 0)

    def test_invalid_name_returns_none(self):
        from sound_recorder.recorder import _parse_partial_started_at

        assert _parse_partial_started_at(Path("recording.m4a")) is None
        assert _parse_partial_started_at(Path("foo.partial.m4a")) is None

    def test_non_partial_suffix(self):
        from sound_recorder.recorder import _parse_partial_started_at

        assert _parse_partial_started_at(Path(".20250315-093000.m4a")) is None


# ---------------------------------------------------------------------------
# ChunkedAudioRecorder static / class methods
# ---------------------------------------------------------------------------

class TestStripAnsi:
    def test_removes_color_codes(self):
        from sound_recorder.recorder import ChunkedAudioRecorder

        raw = "\033[1m\033[36mHello\033[0m world"
        assert ChunkedAudioRecorder._strip_ansi(raw) == "Hello world"

    def test_plain_text_unchanged(self):
        from sound_recorder.recorder import ChunkedAudioRecorder

        assert ChunkedAudioRecorder._strip_ansi("no escapes") == "no escapes"


class TestDbToGain:
    def test_zero_db(self):
        from sound_recorder.recorder import ChunkedAudioRecorder

        assert ChunkedAudioRecorder._db_to_gain(0.0) == pytest.approx(1.0)

    def test_minus_20_db(self):
        from sound_recorder.recorder import ChunkedAudioRecorder

        assert ChunkedAudioRecorder._db_to_gain(-20.0) == pytest.approx(0.1)

    def test_positive_db(self):
        from sound_recorder.recorder import ChunkedAudioRecorder

        assert ChunkedAudioRecorder._db_to_gain(20.0) == pytest.approx(10.0)


class TestDeduplicate:
    def test_no_collision(self, tmp_path):
        from sound_recorder.recorder import ChunkedAudioRecorder

        target = tmp_path / "recording.m4a"
        assert ChunkedAudioRecorder._deduplicate(target) == target

    def test_single_collision(self, tmp_path):
        from sound_recorder.recorder import ChunkedAudioRecorder

        target = tmp_path / "recording.m4a"
        target.touch()
        result = ChunkedAudioRecorder._deduplicate(target)
        assert result == tmp_path / "recording-1.m4a"

    def test_multiple_collisions(self, tmp_path):
        from sound_recorder.recorder import ChunkedAudioRecorder

        target = tmp_path / "recording.m4a"
        target.touch()
        (tmp_path / "recording-1.m4a").touch()
        (tmp_path / "recording-2.m4a").touch()
        result = ChunkedAudioRecorder._deduplicate(target)
        assert result == tmp_path / "recording-3.m4a"


# ---------------------------------------------------------------------------
# Instance method: _ansi_style  (the renamed method that caused the bug)
# ---------------------------------------------------------------------------

class TestAnsiStyle:
    @pytest.fixture()
    def recorder(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        return build_recorder(
            device_id="test-device",
            output_dir=tmp_path / "out",
            segment_minutes=1,
        )

    def test_color_output_enabled(self, recorder):
        recorder.use_color_output = True
        result = recorder._ansi_style("hello", "\033[32m")
        assert "\033[32m" in result
        assert "hello" in result
        assert result.endswith("\033[0m")

    def test_color_output_disabled(self, recorder):
        recorder.use_color_output = False
        assert recorder._ansi_style("hello", "\033[32m") == "hello"

    def test_bold_flag(self, recorder):
        recorder.use_color_output = True
        result = recorder._ansi_style("txt", "\033[36m", bold=True)
        assert "\033[1m" in result  # ANSI_BOLD


# ---------------------------------------------------------------------------
# Session lock helpers
# ---------------------------------------------------------------------------

class TestSessionLock:
    @pytest.fixture()
    def recorder(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        return build_recorder(
            device_id="lock-test",
            output_dir=tmp_path / "out",
            segment_minutes=1,
        )

    def test_write_and_load(self, recorder):
        recorder._write_session_lock()
        data = recorder._load_session_lock()
        assert data is not None
        assert data["pid"] == os.getpid()
        assert data["device_id"] == "lock-test"
        assert data["active_temp_file"] is None

    def test_write_with_temp_path(self, recorder):
        temp = recorder.output_dir / "temp.m4a"
        recorder._write_session_lock(temp)
        data = recorder._load_session_lock()
        assert data["active_temp_file"] == str(temp)

    def test_release_removes_file(self, recorder):
        recorder._write_session_lock()
        assert recorder.session_lock_path.exists()
        recorder._release_session_lock()
        assert not recorder.session_lock_path.exists()

    def test_release_without_owning_is_noop(self, recorder):
        # Should not raise even though there's no file
        recorder._release_session_lock()

    def test_load_returns_none_when_missing(self, recorder):
        assert recorder._load_session_lock() is None

    def test_load_returns_none_on_corrupt_json(self, recorder):
        recorder.session_lock_path.write_text("not json{{{")
        assert recorder._load_session_lock() is None


# ---------------------------------------------------------------------------
# build_recorder factory
# ---------------------------------------------------------------------------

class TestBuildRecorder:
    def test_creates_output_dir(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        out = tmp_path / "nested" / "recordings"
        rec = build_recorder(device_id="d", output_dir=out, segment_minutes=5)
        assert out.exists()
        assert rec.segment_seconds == 300

    def test_segment_minutes_floor(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=0)
        # max(1, 0) * 60 == 60
        assert rec.segment_seconds == 60

    def test_defaults(self, tmp_path):
        from sound_recorder.recorder import build_recorder, SAFE_TARGET_PEAK_DBFS

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=10)
        assert rec.target_peak_dbfs == pytest.approx(SAFE_TARGET_PEAK_DBFS)
        assert rec.stop_requested is False
        assert rec.is_configured is False


class TestCueSidecar:
    def test_write_segment_sidecar_creates_json_payload(self, tmp_path):
        from sound_recorder.recorder import CombinedTrackEvent, build_recorder

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=10)
        started_at = datetime(2026, 5, 20, 9, 15, 0)
        ended_at = datetime(2026, 5, 20, 10, 15, 0)
        audio_path = tmp_path / "19052026-start0915-end1015.m4a"
        rec._collect_combined_track_events = lambda *_args: [
            CombinedTrackEvent(
                observed_at=started_at,
                display_text="09:15 => ARTIST A => Song One",
                artist="ARTIST A",
                title="Song One",
                source="last_state",
                source_path="/tmp/last_state.json",
            ),
            CombinedTrackEvent(
                observed_at=datetime(2026, 5, 20, 9, 30, 0),
                display_text="09:30 => NO DETECTION",
                artist="UNKNOWN",
                title="NO DETECTION",
                source="song_history",
                source_path="/tmp/last_state.json",
                raw_line="2026-05-20 09:30 | Unknown - NO DETECTION",
            ),
        ]

        rec._write_segment_sidecar(audio_path, started_at, ended_at)

        payload = json.loads(audio_path.with_suffix(".cuesheet.json").read_text(encoding="utf-8"))
        cue_text = audio_path.with_suffix(".cue").read_text(encoding="utf-8")
        assert payload["audio_file"] == audio_path.name
        assert payload["segment_started_at"] == "2026-05-20T09:15:00"
        assert payload["segment_ended_at"] == "2026-05-20T10:15:00"
        assert len(payload["tracks"]) == 2
        assert payload["tracks"][0]["offset_seconds"] == 0.0
        assert payload["tracks"][0]["artist"] == "ARTIST A"
        assert payload["tracks"][0]["title"] == "Song One"
        assert payload["tracks"][0]["source"] == "last_state"
        assert payload["tracks"][0]["partial_at_end"] is False
        assert payload["tracks"][1]["offset_seconds"] == 900.0
        assert payload["tracks"][1]["display"] == "09:30 => NO DETECTION"
        assert payload["tracks"][1]["artist"] == "UNKNOWN"
        assert payload["tracks"][1]["title"] == "NO DETECTION"
        assert payload["tracks"][1]["source"] == "song_history"
        assert payload["tracks"][1]["partial_at_end"] is True
        assert 'FILE "19052026-start0915-end1015.m4a" MP4' in cue_text
        assert '  TRACK 01 AUDIO' in cue_text
        assert '    TITLE "Song One"' in cue_text
        assert '    PERFORMER "ARTIST A"' in cue_text
        assert '    INDEX 01 15:00:00' in cue_text

    def test_capture_segment_track_event_deduplicates_same_display(self, tmp_path):
        from sound_recorder.recorder import ActiveSegment, build_recorder

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=10)
        started_at = datetime(2026, 5, 20, 9, 15, 0)
        rec.active_segment = ActiveSegment(started_at=started_at, temp_path=tmp_path / "temp.m4a")
        rec.last_state_display = "09:15 => ARTIST A => Song One"
        rec.last_state_source_path = "/tmp/last_state.json"

        rec._capture_segment_track_event(started_at, force=True)
        rec._capture_segment_track_event(datetime(2026, 5, 20, 9, 16, 0))

        assert len(rec.segment_track_events) == 1

    def test_merge_track_events_keeps_earliest_time_for_same_track(self, tmp_path):
        from sound_recorder.recorder import CombinedTrackEvent, build_recorder

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=10)
        merged = rec._merge_track_events(
            [
                CombinedTrackEvent(
                    observed_at=datetime(2026, 5, 20, 9, 15, 0),
                    display_text="09:15 => ARTIST A => Song One",
                    artist="ARTIST A",
                    title="Song One",
                    source="song_history",
                    source_path="/tmp/song_history.log",
                ),
                CombinedTrackEvent(
                    observed_at=datetime(2026, 5, 20, 9, 15, 5),
                    display_text="09:15 => ARTIST A => Song One",
                    artist="ARTIST A",
                    title="Song One",
                    source="last_state",
                    source_path="/tmp/last_state.json",
                ),
            ]
        )

        assert len(merged) == 1
        assert merged[0].source == "last_state"
        assert merged[0].observed_at == datetime(2026, 5, 20, 9, 15, 0)

    def test_start_segment_refreshes_last_state_before_first_track_event(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=10)
        captured_display = []
        rec._load_last_state_display = lambda: "09:15 => FRESH ARTIST => Fresh Song"
        rec._write_session_lock = lambda *_args, **_kwargs: None
        rec._log_line = lambda *_args, **_kwargs: None

        class DummyAudioOutput:
            def isRecording(self):
                return False

            def startRecordingToOutputFileURL_outputFileType_recordingDelegate_(self, *_args):
                return None

        rec.audio_output = DummyAudioOutput()

        original_capture = rec._capture_segment_track_event

        def capture_with_probe(observed_at, force=False):
            captured_display.append(rec.last_state_display)
            return original_capture(observed_at, force=force)

        rec._capture_segment_track_event = capture_with_probe
        rec._start_segment()

        assert captured_display[0] == "09:15 => FRESH ARTIST => Fresh Song"
        assert rec.segment_track_events[0].display_text == "09:15 => FRESH ARTIST => Fresh Song"

    def test_format_cue_index_uses_cd_frames(self):
        from sound_recorder.recorder import ChunkedAudioRecorder

        assert ChunkedAudioRecorder._format_cue_index(0.0) == "00:00:00"
        assert ChunkedAudioRecorder._format_cue_index(1.0) == "00:01:00"
        assert ChunkedAudioRecorder._format_cue_index(90.5) == "01:30:38"


# ---------------------------------------------------------------------------
# Recover stale partials
# ---------------------------------------------------------------------------

class TestRecoverStalePartials:
    def test_recovers_partial_file(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        partial = tmp_path / ".20250315-093000.partial.m4a"
        partial.write_bytes(b"\x00" * 128)

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=1)
        rec._recover_stale_partials()

        assert not partial.exists()
        recovered = [f for f in tmp_path.iterdir() if f.suffix == ".m4a" and "partial" not in f.name]
        assert len(recovered) == 1

    def test_ignores_non_partial_files(self, tmp_path):
        from sound_recorder.recorder import build_recorder

        normal = tmp_path / "recording.m4a"
        normal.write_bytes(b"\x00" * 64)

        rec = build_recorder(device_id="d", output_dir=tmp_path, segment_minutes=1)
        rec._recover_stale_partials()

        assert normal.exists()
