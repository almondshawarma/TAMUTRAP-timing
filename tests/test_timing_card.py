"""Tests for the hardware wrapper that don't need real hardware.

The SpinAPI calls are exercised through a tiny fake so the status-decode and
dry-run behavior are covered without a PulseBlaster attached.
"""
import pandas as pd

from timing_card import TimingCard, connect_or_dry_run


class _FakeAPI:
    """Minimal stand-in for the vendor spinapi module."""
    us = 1

    def __init__(self, status_bits: int):
        self._status = status_bits
        self.started = self.stopped = False

    def pb_read_status(self):
        return self._status

    def pb_status_message(self):
        return "fake status"


def _live_card(status_bits: int) -> TimingCard:
    card = TimingCard(enabled=True)
    card._spinapi = _FakeAPI(status_bits)
    card._initialized = True
    return card


def test_min_instruction_us_default_clock():
    card = TimingCard(enabled=False)
    assert card.min_instruction_us == 0.05          # 5 cycles / 100 MHz


def test_min_instruction_us_scales_with_clock():
    card = TimingCard(enabled=False, core_clock_mhz=250.0)
    assert card.min_instruction_us == 5 / 250.0


def test_read_status_dry_run_is_none():
    assert TimingCard(enabled=False).read_status() is None


def test_read_status_running():
    st = _live_card(0x4).read_status()          # running
    assert st["running"] is True
    assert st["flags"] == ["running"]


def test_read_status_stopped():
    st = _live_card(0x1).read_status()          # stopped
    assert st["running"] is False
    assert "stopped" in st["flags"]


def test_read_status_running_and_waiting():
    st = _live_card(0x4 | 0x8).read_status()
    assert set(st["flags"]) == {"running", "waiting"}


def test_apply_is_noop_in_dry_run():
    # Should not raise even with a trivial regions frame.
    regions = pd.DataFrame({
        "Region": [0, 1], "Start": [0.0, 5.0], "Middle": [2.5, 7.5],
        "End": [5.0, 10.0], "Duration": [5.0, 5.0], "State": [0, 1],
    })
    TimingCard(enabled=False).apply(regions)


def test_connect_or_dry_run_falls_back_without_hardware():
    # No spinapi/DLL in the test env, so this must degrade to a dry-run card
    # with a warning message rather than raising.
    card, warning = connect_or_dry_run()
    assert card.enabled is False
    assert warning
