"""Tests for the sound_recorder.cli module."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from sound_recorder.cli import build_parser, _positive_float, _negative_dbfs
from sound_recorder.playlist import (
    build_amber_box_lines,
    build_green_status_line,
    build_script_launch_command,
    discover_playlist_script_candidates,
    find_last_state_file,
    find_song_history_log,
    read_last_state_entry,
    read_last_song_history_entry,
    sanitize_song_history_entry,
)


# ---------------------------------------------------------------------------
# Argument validator functions
# ---------------------------------------------------------------------------

class TestPositiveFloat:
    def test_valid(self):
        assert _positive_float("3.5") == 3.5
        assert _positive_float("0.01") == 0.01

    def test_zero_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _positive_float("0")

    def test_negative_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _positive_float("-1.0")

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError):
            _positive_float("abc")


class TestNegativeDbfs:
    def test_valid(self):
        assert _negative_dbfs("-9.0") == -9.0
        assert _negative_dbfs("-0.1") == -0.1

    def test_zero_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _negative_dbfs("0")

    def test_positive_rejected(self):
        with pytest.raises(argparse.ArgumentTypeError):
            _negative_dbfs("3.0")


# ---------------------------------------------------------------------------
# Argument parser defaults
# ---------------------------------------------------------------------------

class TestBuildParser:
    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.segment_minutes == 60
        assert args.arming_duration == 3.0
        assert args.target_peak_dbfs == -9.0
        assert args.warning_peak_dbfs == -3.0
        assert args.list_devices is False

    def test_custom_segment_minutes(self):
        args = build_parser().parse_args(["--segment-minutes", "30"])
        assert args.segment_minutes == 30

    def test_list_devices_flag(self):
        args = build_parser().parse_args(["--list-devices"])
        assert args.list_devices is True

    def test_all_options(self):
        args = build_parser().parse_args([
            "--output-dir", "/tmp/rec",
            "--segment-minutes", "15",
            "--arming-duration", "5.0",
            "--target-peak-dbfs", "-12.0",
            "--warning-peak-dbfs", "-6.0",
        ])
        assert str(args.output_dir) == "/tmp/rec"
        assert args.segment_minutes == 15
        assert args.arming_duration == 5.0
        assert args.target_peak_dbfs == -12.0
        assert args.warning_peak_dbfs == -6.0


class TestPlaylistHelpers:
    def test_discover_playlist_script_candidates_prefers_keyword_matches(self, tmp_path):
        docs = tmp_path / "Documents"
        docs.mkdir()
        target = docs / "start_playlist.command"
        target.write_text("echo hi", encoding="utf-8")
        (docs / "other.sh").write_text("echo nope", encoding="utf-8")

        candidates = discover_playlist_script_candidates(docs)
        assert candidates == [target]

    def test_find_song_history_log_uses_remembered_path(self, tmp_path):
        remembered = tmp_path / "song_history.log"
        remembered.write_text("track", encoding="utf-8")

        found = find_song_history_log(tmp_path, remembered_path=remembered)
        assert found == remembered

    def test_find_song_history_log_scans_documents_and_script_directory(self, tmp_path):
        docs = tmp_path / "Documents"
        scripts = docs / "playlist"
        scripts.mkdir(parents=True)
        script_path = scripts / "start_playlist.sh"
        script_path.write_text("echo hi", encoding="utf-8")
        history = scripts / "song_history.txt"
        history.write_text("Artist - Song https://example.com", encoding="utf-8")

        found = find_song_history_log(docs, script_path=script_path)
        assert found == history

    def test_find_last_state_file_scans_documents_and_script_directory(self, tmp_path):
        docs = tmp_path / "Documents"
        scripts = docs / "playlist"
        scripts.mkdir(parents=True)
        script_path = scripts / "start_playlist.sh"
        script_path.write_text("echo hi", encoding="utf-8")
        last_state = scripts / "last_state.json"
        last_state.write_text("{}", encoding="utf-8")

        found = find_last_state_file(docs, script_path=script_path)
        assert found == last_state

    def test_read_last_song_history_entry_strips_url(self, tmp_path):
        history = tmp_path / "song_history.log"
        history.write_text(
            "Earlier entry\nLatest Artist - Great Song https://example.com/watch?v=1\n",
            encoding="utf-8",
        )

        assert read_last_song_history_entry(history) == "Latest Artist - Great Song"

    def test_read_last_state_entry_formats_time_artist_and_title(self, tmp_path):
        last_state = tmp_path / "last_state.json"
        last_state.write_text(
            '{"timestamp": "2026-05-20T18:45:00Z", "artist": "Nils Frahm", "title": "Says"}',
            encoding="utf-8",
        )

        entry = read_last_state_entry(last_state)
        assert entry is not None
        assert " => NILS FRAHM => Says" in entry
        assert len(entry.split(" => ")[0]) == 5

    def test_read_last_state_entry_null_artist_and_title_show_no_detection(self, tmp_path):
        last_state = tmp_path / "last_state.json"
        last_state.write_text(
            '{"timestamp": "2026-05-20T18:45:00Z", "artist": null, "title": null}',
            encoding="utf-8",
        )

        assert read_last_state_entry(last_state) == "18:45 => NO DETECTION"

    def test_sanitize_song_history_entry_handles_plain_text(self):
        assert sanitize_song_history_entry("Artist | Track") == "Artist | Track"

    def test_build_amber_box_lines_truncates_to_width(self):
        lines = build_amber_box_lines("X" * 40, max_width=20)
        assert len(lines) == 3
        assert lines[0].startswith("+-")
        assert lines[1].startswith("| ")
        assert len(lines[1]) <= 20

    def test_build_green_status_line_truncates_to_width(self):
        line = build_green_status_line("X" * 40, max_width=20)
        assert len(line) <= 18

    def test_build_script_launch_command_uses_python_for_py_files(self):
        command = build_script_launch_command(Path("/tmp/playlist.py"))
        assert command.startswith("/usr/bin/env python3 ")

    def test_build_script_launch_command_uses_zsh_for_shell_files(self):
        command = build_script_launch_command(Path("/tmp/playlist.command"))
        assert command.startswith("/bin/zsh ")
