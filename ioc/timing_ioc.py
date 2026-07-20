"""EPICS IOC for the TAMUTRAP timing pulse-timing controller.

This is a *thin adapter*, mapping EPICS Process Variables (PVs) onto the
existing pure model (`core.py`) and hardware driver (`timing_card.py`). No
pulse math or hardware logic is reimplemented here, as the IOC imports it.

Notes
------------
* EPICS namespaces are STATIC: PVs are declared at IOC start, not added at
  runtime. So all 24 channels are pre-declared as CH0..CH23 SubGroups, and a
  per-channel ``Active`` flag says which are in use. "Add channel" in the old
  tkinter app == flipping a pre-declared slot Active.
* Editing doesn't touch card: edit PVs only mutate an in-memory staging
  model and re-validate via ``PulseConfig``. A rejected edit is refused at the
  PV (the client sees a write error) and the old value stands. Only ``Push``
  programs hardware.
* Write-only readback: the PulseBlaster cannot report its loaded pattern. As
  the *sole writer*, this IOC records what it last pushed into ``Loaded*`` PVs
  and an autosave file, so "what's loaded" survives restarts. Those PVs are the
  commanded value, not a read of the chip.

Run it (dry-run)::

    python ioc/timing_ioc.py --list-pvs
    # then, in another shell:
    caget TAMUTRAP:timing:Period
    caput TAMUTRAP:timing:CH0:Active 1
    caput TAMUTRAP:timing:CH0:Dur 5
    caput TAMUTRAP:timing:Push 1
    camonitor TAMUTRAP:timing:RunState
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# --- make the existing pure model importable without installing the package ---
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd  # noqa: E402  (after sys.path shim)

from caproto.server import PVGroup, SubGroup, ioc_arg_parser, pvproperty, run  # noqa: E402

from core import (  # noqa: E402
    MAX_CHANNELS,
    PulseConfig,
    PulseFormatError,
    create_regions,
    load_pulses,
    short_regions,
)
from timing_card import connect_or_dry_run  # noqa: E402

_AUTOSAVE = Path(__file__).resolve().parent / "timing_autosave.json"
_DEFAULT_PULSES = _SRC / "pulses" / "pulses3.txt"


# ---------------------------------------------------------------------------
# Per-channel PV group. One instance per pre-declared channel CH0..CH23.
# ---------------------------------------------------------------------------
class ChannelGroup(PVGroup):
    active = pvproperty(value=False, name="Active",
                        doc="Channel is part of the current config")
    start = pvproperty(value=0.0, name="Start", units="us", precision=3,
                       doc="Pulse start [us]")
    dur = pvproperty(value=0.0, name="Dur", units="us", precision=3,
                     doc="Pulse duration [us]")
    end = pvproperty(value=0.0, name="End", units="us", precision=3, read_only=True,
                     doc="Derived: start + duration [us]")
    invert = pvproperty(value=True, name="Invert",
                        doc="Inverted: idle high, pulse low")
    label = pvproperty(value="", name="Label", max_length=64, report_as_string=True,
                       doc="Connection label (no spaces)")

    # last-pushed mirror (see module docstring: commanded value, not a chip read)
    loaded_start = pvproperty(value=0.0, name="LoadedStart", units="us",
                              precision=3, read_only=True)
    loaded_dur = pvproperty(value=0.0, name="LoadedDur", units="us",
                            precision=3, read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        m = re.search(r"CH(\d+):", self.prefix)
        self.index = int(m.group(1)) if m else 0

    @active.putter
    async def active(self, instance, value):
        return await self.parent.edit(self.index, "active", bool(value), instance)

    @start.putter
    async def start(self, instance, value):
        return await self.parent.edit(self.index, "start", float(value), instance)

    @dur.putter
    async def dur(self, instance, value):
        return await self.parent.edit(self.index, "dur", float(value), instance)

    @invert.putter
    async def invert(self, instance, value):
        return await self.parent.edit(self.index, "invert", bool(value), instance)

    @label.putter
    async def label(self, instance, value):
        return await self.parent.edit(self.index, "label", str(value), instance)


# ---------------------------------------------------------------------------
# Top-level IOC
# ---------------------------------------------------------------------------
class timingIOC(PVGroup):
    period = pvproperty(value=1000.0, name="Period", units="us", precision=2,
                        doc="Total repeating cycle period [us]")
    push = pvproperty(value=False, name="Push",
                      doc="Program the card: stop -> program -> start")
    run_state = pvproperty(value="unknown", name="RunState", max_length=40,
                           read_only=True, report_as_string=True,
                           doc="Card run state (running/stopped/waiting/reset/dry-run)")
    running = pvproperty(value=False, name="Running", read_only=True,
                         doc="True hardware read: is it running")
    unpushed_edits = pvproperty(value=False, name="UnpushedEdits", read_only=True,
                                doc="In-memory config differs from what was pushed")
    n_regions = pvproperty(value=0, name="NRegions", read_only=True,
                           doc="Instruction/region count of last build")
    core_clock_mhz = pvproperty(value=100.0, name="CoreClockMHz", units="MHz",
                                precision=1, doc="PulseBlaster core clock [MHz]")
    min_region_us = pvproperty(value=0.05, name="MinRegionUs", units="us",
                               precision=4, read_only=True,
                               doc="Shortest region the card can represent [us]")
    short_region_warn = pvproperty(value=False, name="ShortRegionWarn", read_only=True,
                                   doc="Last build had a sub-minimum region")
    msg = pvproperty(value="", name="Msg", max_length=200, read_only=True,
                     report_as_string=True, doc="Last status/validation message")
    loaded_time = pvproperty(value="never", name="LoadedTime", max_length=40,
                             read_only=True, report_as_string=True,
                             doc="Timestamp of the last successful push")

    # Pre-declare all 24 channels (static namespace). caproto supports building
    # repeated SubGroups by assigning into the class namespace in a loop.
    for _i in range(MAX_CHANNELS):
        locals()[f"ch{_i}"] = SubGroup(ChannelGroup, prefix=f"CH{_i}:")
    del _i

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Staging: full 24-wide arrays, independent of how many are active
        self.s_active = [False] * MAX_CHANNELS
        self.s_start = [0.0] * MAX_CHANNELS
        self.s_dur = [0.0] * MAX_CHANNELS
        self.s_invert = [True] * MAX_CHANNELS
        self.s_label = [""] * MAX_CHANNELS
        self._period = 1000.0
        self._loading = False
        # Connect to hardware, or fall back to a dry-run card (no spinapi needed)
        self.card, self._card_msg = connect_or_dry_run()
        self._load_initial()

    # ---- initial state: autosave if present, else the shipped example file ----
    def _load_initial(self) -> None:
        if _AUTOSAVE.exists():
            try:
                data = json.loads(_AUTOSAVE.read_text())
                self._period = float(data["period"])
                for row in data["channels"]:
                    i = int(row["channel"])
                    self.s_active[i] = True
                    self.s_start[i] = float(row["start"])
                    self.s_dur[i] = float(row["duration"])
                    self.s_invert[i] = bool(row["invert"])
                    self.s_label[i] = str(row.get("connection", ""))
                return
            except (KeyError, ValueError, TypeError):
                pass  # corrupt autosave -> fall through to the example file
        if _DEFAULT_PULSES.exists():
            cfg = load_pulses(str(_DEFAULT_PULSES))
            self._period = cfg.period
            for row in cfg.pulses.itertuples(index=False):
                i = int(row.Channel)
                if 0 <= i < MAX_CHANNELS:
                    self.s_active[i] = True
                    self.s_start[i] = float(row.Start)
                    self.s_dur[i] = float(row.Duration)
                    self.s_invert[i] = bool(row.Invert)
                    self.s_label[i] = str(getattr(row, "Connection", ""))

    # ---- build a PulseConfig from active channels (the correctness core) ----
    def _build_config(self, period: float | None = None) -> PulseConfig:
        rows = [
            {"Channel": i, "Connection": self.s_label[i], "Invert": self.s_invert[i],
             "Start": self.s_start[i], "Duration": self.s_dur[i]}
            for i in range(MAX_CHANNELS) if self.s_active[i]
        ]
        if not rows:
            raise PulseFormatError("No active channels.")
        df = pd.DataFrame(rows)
        return PulseConfig(pulses=df, period=self._period if period is None else period)

    # ---- one edit path for every channel field, with validate-or-reject ----
    async def edit(self, index: int, field: str, value, instance):
        staging = {"active": self.s_active, "start": self.s_start, "dur": self.s_dur,
                   "invert": self.s_invert, "label": self.s_label}[field]
        if self._loading:            # startup priming: accept without side effects
            staging[index] = value
            return value
        old = staging[index]
        staging[index] = value
        try:
            # Only revalidate when there's at least one active channel, an all-off
            # config is allowed to exist (nothing to push yet)
            if any(self.s_active):
                self._build_config()
        except PulseFormatError as exc:
            staging[index] = old
            await self.msg.write(f"Rejected CH{index} {field}={value}: {exc}")
            raise  # refuse the PV write, client sees an error, old value stands
        # accepted
        if field in ("start", "dur"):
            await getattr(self, f"ch{index}").end.write(self.s_start[index] + self.s_dur[index])
        await self.unpushed_edits.write(True)
        await self.msg.write(f"CH{index} {field} = {value}")
        return value

    @period.putter
    async def period(self, instance, value):
        old = self._period
        self._period = float(value)
        try:
            if any(self.s_active):
                self._build_config()
        except PulseFormatError as exc:
            self._period = old
            await self.msg.write(f"Rejected period={value}: {exc}")
            raise
        if not self._loading:
            await self.unpushed_edits.write(True)
        return float(value)

    @core_clock_mhz.putter
    async def core_clock_mhz(self, instance, value):
        self.card.core_clock_mhz = float(value)
        await self.min_region_us.write(self.card.min_instruction_us)
        return float(value)

    @push.putter
    async def push(self, instance, value):
        if not value:
            return False
        try:
            cfg = self._build_config()
        except PulseFormatError as exc:
            await self.msg.write(f"Push refused: {exc}")
            raise
        regions = create_regions(cfg.pulses, cfg.period)
        shorts = short_regions(regions, self.card.min_instruction_us)
        await self.short_region_warn.write(not shorts.empty)
        # program the card (a no-op in dry-run mode)
        self.card.apply(regions)
        # record what we just pushed: Loaded* mirror + autosave (see docstring)
        for i in range(MAX_CHANNELS):
            ch = getattr(self, f"ch{i}")
            await ch.loaded_start.write(self.s_start[i] if self.s_active[i] else 0.0)
            await ch.loaded_dur.write(self.s_dur[i] if self.s_active[i] else 0.0)
        self._save_autosave(cfg)
        await self.n_regions.write(len(regions))
        await self.loaded_time.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        await self.unpushed_edits.write(False)
        await self.msg.write(f"Pushed {len(regions)} regions"
                             + (" (WARNING: sub-minimum region present)" if not shorts.empty else ""))
        await self._refresh_run_state()
        return False  # momentary: reset the button

    def _save_autosave(self, cfg: PulseConfig) -> None:
        data = {
            "period": cfg.period,
            "channels": [
                {"channel": int(r.Channel), "connection": str(getattr(r, "Connection", "")),
                 "start": float(r.Start), "duration": float(r.Duration), "invert": bool(r.Invert)}
                for r in cfg.pulses.itertuples(index=False)
            ],
        }
        _AUTOSAVE.write_text(json.dumps(data, indent=2))

    async def _refresh_run_state(self) -> None:
        st = self.card.read_status()
        if st is None:  # dry-run card
            await self.run_state.write("dry-run")
            await self.running.write(False)
        else:
            await self.run_state.write(", ".join(st["flags"]) or "unknown")
            await self.running.write(bool(st["running"]))

    # ---- push initial staging into the PVs, and start the run-state poll ----
    @push.startup
    async def push(self, instance, async_lib):
        self._loading = True
        await self.period.write(self._period)
        await self.min_region_us.write(self.card.min_instruction_us)
        await self.core_clock_mhz.write(self.card.core_clock_mhz)
        for i in range(MAX_CHANNELS):
            ch = getattr(self, f"ch{i}")
            await ch.active.write(self.s_active[i])
            await ch.start.write(self.s_start[i])
            await ch.dur.write(self.s_dur[i])
            await ch.end.write(self.s_start[i] + self.s_dur[i])
            await ch.invert.write(self.s_invert[i])
            await ch.label.write(self.s_label[i])
        self._loading = False
        await self.unpushed_edits.write(False)
        await self.msg.write(self._card_msg.splitlines()[0] if self._card_msg else "IOC ready")
        await self._refresh_run_state()

    @run_state.scan(period=1.0)
    async def run_state(self, instance, async_lib):
        await self._refresh_run_state()


def main() -> None:
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="TAMUTRAP:timing:",
        desc="TAMUTRAP pulse-timing EPICS IOC (dry-run safe).",
    )
    ioc = timingIOC(**ioc_options)
    run(ioc.pvdb, **run_options)


if __name__ == "__main__":
    main()
