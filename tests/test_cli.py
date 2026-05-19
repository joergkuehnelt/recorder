"""Tests for the sound_recorder.cli module."""
from __future__ import annotations

import argparse

import pytest

from sound_recorder.cli import build_parser, _positive_float, _negative_dbfs


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
