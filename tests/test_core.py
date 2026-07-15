"""Regression + unit tests for the pulse data model.

The headline test pins the region/state output of the real ``pulses3.txt``
cycle: if a future refactor of ``create_regions`` changes what gets
programmed onto the card, this fails loudly instead of silently shipping
different timing to the trap.

Run from the repo root:

    pip install pytest
    pytest
"""
from pathlib import Path

import pandas as pd
import pytest

import core
from core import (
    PulseConfig, PulseFormatError, load_pulses, save_pulses,
    create_regions, channel_is_on, short_regions,
)

PULSE_FILE = str(Path(core.__file__).parent / "pulses" / "pulses3.txt")


# ── Golden-master: the real pulses3.txt cycle ────────────────────────────────

# Captured from the known-good build. Any change here is a change to what the
# hardware would actually output -- treat a failure as "prove the new output
# is correct and re-pin", never as "just update the number".
EXPECTED_STATES = [8190, 8191, 8187, 7835, 7833, 8185, 8189, 8191,
                   7167, 8191, 2047, 4095, 4087, 8183, 7527, 7535, 8191]


@pytest.fixture
def cfg() -> PulseConfig:
    return load_pulses(PULSE_FILE)


def test_pulses3_loads(cfg):
    assert cfg.period == 600000.0
    assert len(cfg.pulses) == 13
    assert list(cfg.pulses["Channel"]) == list(range(13))


def test_pulses3_region_states_are_pinned(cfg):
    regions = cfg.regions()
    assert regions["State"].tolist() == EXPECTED_STATES


def test_region_durations_tile_the_full_period(cfg):
    regions = cfg.regions()
    # Regions partition [0, period] with no gaps or overlaps.
    assert regions["Duration"].sum() == pytest.approx(cfg.period)
    assert regions["Start"].iloc[0] == 0.0
    assert regions["End"].iloc[-1] == pytest.approx(cfg.period)


# ── channel_is_on / invert logic ─────────────────────────────────────────────

def test_channel_is_on_non_inverted():
    assert channel_is_on(5.0, 0.0, 10.0, invert=False) == 1
    assert channel_is_on(15.0, 0.0, 10.0, invert=False) == 0


def test_channel_is_on_inverted():
    assert channel_is_on(5.0, 0.0, 10.0, invert=True) == 0
    assert channel_is_on(15.0, 0.0, 10.0, invert=True) == 1


def test_state_bit_packing():
    # One non-inverted pulse on channel 3, on for the whole window -> bit 3.
    df = pd.DataFrame({"Channel": [3], "Invert": [False],
                       "Start": [0.0], "Duration": [10.0]})
    regions = create_regions(df, period=10.0)
    assert regions["State"].tolist() == [1 << 3]


# ── add / remove channels ────────────────────────────────────────────────────

def test_add_channel(cfg):
    cfg.add_channel(20, connection="Test_Probe", start=1.0, duration=2.0, invert=False)
    assert 20 in set(cfg.pulses["Channel"])
    row = cfg.pulses[cfg.pulses["Channel"] == 20].iloc[0]
    assert row["End"] == pytest.approx(3.0)


def test_add_duplicate_channel_rejected(cfg):
    with pytest.raises(PulseFormatError):
        cfg.add_channel(0, connection="dup")


def test_remove_channel(cfg):
    cfg.remove_channel(0)
    assert 0 not in set(cfg.pulses["Channel"])
    assert len(cfg.pulses) == 12


# ── validation ───────────────────────────────────────────────────────────────

def test_pulse_past_period_rejected():
    df = pd.DataFrame({"Channel": [0], "Invert": [True],
                       "Start": [0.0], "Duration": [100.0]})
    with pytest.raises(PulseFormatError):
        PulseConfig(pulses=df, period=50.0)


def test_negative_period_rejected(cfg):
    with pytest.raises(PulseFormatError):
        cfg.set_period(-1.0)


def test_rejected_period_leaves_model_unchanged(cfg):
    # Shrinking the period below a pulse's end must be rejected AND must not
    # leave the config holding the bad period (transactional set_period).
    good = cfg.period
    with pytest.raises(PulseFormatError):
        cfg.set_period(100.0)          # far shorter than the extraction pulses
    assert cfg.period == good
    # Still fully usable afterwards.
    assert cfg.regions()["State"].tolist() == EXPECTED_STATES


# ── short-region detection (hardware minimum) ────────────────────────────────

def test_short_regions_flags_sub_minimum():
    # Two channels whose edges sit 0.01 us apart -> a 0.01 us region.
    df = pd.DataFrame({
        "Channel": [0, 1],
        "Invert": [False, False],
        "Start": [0.0, 0.01],
        "Duration": [5.0, 5.0],
    })
    regions = create_regions(df, period=10.0)
    flagged = short_regions(regions, min_duration_us=0.05)
    assert (flagged["Duration"] < 0.05).all()
    assert len(flagged) >= 1


def test_short_regions_none_when_all_wide(cfg):
    regions = cfg.regions()
    assert len(short_regions(regions, min_duration_us=0.001)) == 0


# ── round-trip save/load ─────────────────────────────────────────────────────

def test_save_load_roundtrip(cfg, tmp_path):
    out = tmp_path / "roundtrip.txt"
    save_pulses(cfg, str(out))
    reloaded = load_pulses(str(out))
    assert reloaded.period == cfg.period
    assert reloaded.regions()["State"].tolist() == EXPECTED_STATES
