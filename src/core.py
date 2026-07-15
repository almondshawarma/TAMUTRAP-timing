"""Core data model for the TAMUTRAP pulse timing system.

This module is pure Python / pandas, withno GUI dependency or
hardware dependency. Everything in here can be unit tested without a
display, a card, or a GUI toolkit installed. The GUI and the hardware 
driver both import from here.
other way around.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dataclasses import dataclass
from typing import Optional

REQUIRED_PULSE_COLUMNS = ("Channel", "Invert", "Start", "Duration")
MAX_CHANNELS = 24


class PulseFormatError(ValueError):
    """Raised when a pulse table or settings file is invalid or incomplete."""


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "t", "1", "yes", "y"}:
            return True
        if text in {"false", "f", "0", "no", "n"}:
            return False
    raise PulseFormatError(f"Could not parse boolean value: {value!r}")


def normalize_pulses(df: pd.DataFrame) -> pd.DataFrame:
    """Return a clean, typed, sorted copy of a pulse table.

    Channel identity lives in the ``Channel``column, not the dataframe
    index. The index is reset to 0..N-1 for display convenience, so
    row position does not equal "channel number".
    """
    out = df.copy()

    missing = [c for c in REQUIRED_PULSE_COLUMNS if c not in out.columns]
    if missing:
        raise PulseFormatError(f"Missing required pulse columns: {missing}")

    out["Channel"] = out["Channel"].astype(int)
    out["Start"] = out["Start"].astype(float)
    out["Duration"] = out["Duration"].astype(float)
    out["Invert"] = out["Invert"].map(_parse_bool)
    out["End"] = out["Start"] + out["Duration"]

    if out["Channel"].duplicated().any():
        dupes = out.loc[out["Channel"].duplicated(), "Channel"].tolist()
        raise PulseFormatError(f"Duplicate channel numbers: {dupes}")
    if (out["Channel"] < 0).any() or (out["Channel"] >= MAX_CHANNELS).any():
        raise PulseFormatError(f"Channel numbers must be in [0, {MAX_CHANNELS}).")

    out = out.sort_values(by="Channel").reset_index(drop=True)

    preferred = ["Channel", "Connection", "Invert", "Start", "End", "Duration"]
    ordered = [c for c in preferred if c in out.columns]
    ordered += [c for c in out.columns if c not in ordered]
    return out[ordered]


@dataclass
class PulseConfig:
    """A pulse table plus the total repeating cycle period in microseconds.

    This is the "source of truth", every mutation
    method re-validates after the change, so this object can never be
    left in an inconsistent state. A bad edit raises PulseFormatError
    and nothing is left half-applied.
    """

    pulses: pd.DataFrame
    period: float

    def __post_init__(self) -> None:
        self.period = float(self.period)
        self.pulses = normalize_pulses(self.pulses)
        self.validate()

    def validate(self) -> None:
        if self.period <= 0:
            raise PulseFormatError("Total period must be positive.")
        if (self.pulses["Duration"] < 0).any():
            raise PulseFormatError("Pulse durations must be non-negative.")
        if (self.pulses["Start"] < 0).any():
            raise PulseFormatError("Pulse starts must be non-negative.")
        bad = self.pulses[self.pulses["End"] > self.period]
        if not bad.empty:
            raise PulseFormatError(
                "One or more pulses extend past the total period. "
                f"Channels: {bad['Channel'].tolist()}"
            )

    def recompute(self) -> pd.DataFrame:
        """Re-derive End and re-validate, call after any raw mutation."""
        self.pulses = normalize_pulses(self.pulses)
        self.validate()
        return self.pulses

    def set_value(self, row: int, column: str, value) -> None:
        self.pulses.at[row, column] = value
        self.recompute()

    def increment(self, row: int, column: str, amount: float) -> None:
        self.pulses.at[row, column] = float(self.pulses.at[row, column]) + float(amount)
        self.recompute()

    def set_period(self, period: float) -> None:
        # Transactional: a rejected period (e.g. shorter than a pulse's end)
        # must leave the object exactly as it was, not holding the bad value.
        old = self.period
        self.period = float(period)
        try:
            self.recompute()
        except PulseFormatError:
            self.period = old
            raise

    def add_channel(self, channel: int, connection: str = "", start: float = 0.0,
                     duration: float = 0.0, invert: bool = True) -> None:
        if channel in self.pulses["Channel"].values:
            raise PulseFormatError(f"Channel {channel} already exists.")
        new_row = {
            "Channel": channel, "Connection": connection,
            "Start": start, "Duration": duration, "Invert": invert,
        }
        self.pulses = pd.concat([self.pulses, pd.DataFrame([new_row])], ignore_index=True)
        self.recompute()

    def remove_channel(self, channel: int) -> None:
        self.pulses = self.pulses[self.pulses["Channel"] != channel].reset_index(drop=True)
        self.recompute()

    def regions(self) -> pd.DataFrame:
        """The timing regions implied by the current pulses and period."""
        return create_regions(self.pulses, self.period)


def channel_is_on(mid: float, start: float, end: float, invert: bool) -> int:
    """Return the binary output bit for one channel at one instant."""
    inside_pulse = start < mid < end
    if invert:
        return 0 if inside_pulse else 1
    return 1 if inside_pulse else 0


def create_regions(pulses: pd.DataFrame, period: float) -> pd.DataFrame:
    """Convert per-channel pulse intervals into hardware-ready regions.

    Every pulse start/end across every channel becomes a cut point on the
    timeline. Within each resulting region the combined channel state is
    constant, so each region maps to exactly one PulseBlaster instruction.
    State is sampled at the region midpoint and packed as
    sum(bit_c * 2**channel_c) across all channels.
    """
    table = normalize_pulses(pulses)
    period = float(period)

    edges = np.concatenate((table["Start"].to_numpy(), table["End"].to_numpy(), [0.0, period]))
    edges = np.unique(edges.astype(float))
    edges = edges[(edges >= 0.0) & (edges <= period)]
    if len(edges) < 2:
        raise PulseFormatError("Not enough timing edges to build regions.")

    regions = pd.DataFrame({"Start": edges[:-1], "End": edges[1:]})
    regions["Region"] = np.arange(len(regions), dtype=int)
    regions["Middle"] = 0.5 * (regions["Start"] + regions["End"])
    regions["Duration"] = regions["End"] - regions["Start"]

    states: list[int] = []
    for mid in regions["Middle"]:
        state = 0
        for pulse in table.itertuples(index=False):
            bit = channel_is_on(float(mid), float(pulse.Start), float(pulse.End), bool(pulse.Invert))
            state += bit * (1 << int(pulse.Channel))
        states.append(int(state))
    regions["State"] = states
    return regions[["Region", "Start", "Middle", "End", "Duration", "State"]]


def short_regions(regions: pd.DataFrame, min_duration_us: float) -> pd.DataFrame:
    """Return the regions whose duration is below the card's minimum.

    A PulseBlaster instruction has a hardware minimum length (a handful of
    core-clock cycles). Two pulse edges that fall closer together than that
    collapse into a region the card cannot represent, so the programmed
    timing would silently disagree with what's on screen. Callers use this
    to warn before pushing. A zero-length region (two coincident edges) is
    dropped by ``create_regions`` upstream and never reaches here.
    """
    if "Duration" not in regions.columns:
        raise PulseFormatError("regions dataframe has no Duration column.")
    return regions[regions["Duration"] < float(min_duration_us)]


def load_pulses(fname: str) -> PulseConfig:
    """Load the v4 fixed-width pulse settings file format."""
    period: Optional[float] = None
    header_line: Optional[int] = None

    with open(fname, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if "Total Period" in line:
            try:
                period = float(lines[i + 1].strip())
            except (IndexError, ValueError) as exc:
                raise PulseFormatError("Could not read Total Period value.") from exc
        if header_line is None and "Channel" in line:
            header_line = i

    if period is None:
        raise PulseFormatError("Could not find 'Total Period' in pulse file.")
    if header_line is None:
        raise PulseFormatError("Could not find pulse table header containing 'Channel'.")

    pulses = pd.read_fwf(fname, header=0, skiprows=header_line)
    return PulseConfig(pulses=pulses, period=period)


def save_pulses(config: PulseConfig, fname: str) -> None:
    """Write a pulse config back out in the v4 fixed-width format."""
    cols = ["Channel", "Connection", "Start", "Duration", "Invert"]
    missing = [c for c in cols if c not in config.pulses.columns]
    if missing:
        raise PulseFormatError(f"Cannot save pulse file. Missing columns: {missing}")

    pulses = config.pulses[cols].copy()
    pulses["Channel"] = pulses["Channel"].astype(int)
    pulses["Connection"] = pulses["Connection"].astype(str)
    pulses["Start"] = pulses["Start"].astype(float)
    pulses["Duration"] = pulses["Duration"].astype(float)
    pulses["Invert"] = pulses["Invert"].astype(bool)

    with open(fname, "w", encoding="utf-8") as f:
        f.write("#== All times are in microseconds (us). Connection labels MUST NOT have any     ==#\n")
        f.write("#== whitespace. Use underscores instead. This file's format is fairly specific. ==#\n")
        f.write("#== You can manually add channels (up to 24) and widen the column spacing, but  ==#\n")
        f.write("#== nothing more really. Inversion is \"True\"/\"False\".                           ==#\n\n")
        f.write("Total Period\n")
        f.write(f"{config.period:.2f}\n\n")
        f.write(
            f"{'Channel':>7}   {'Connection':<24} {'Start':>14} {'Duration':>14} {'Invert':>8}\n"
        )
        for p in pulses.itertuples(index=False):
            f.write(
                f"{p.Channel:7d}   {p.Connection:<24} {p.Start:14.2f} "
                f"{p.Duration:14.2f} {str(p.Invert):>8}\n"
            )
