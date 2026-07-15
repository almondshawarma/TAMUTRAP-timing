"""Tkinter GUI for the TAMUTRAP pulse timing controller.

This is a working skeleton: editing, validation, plotting (with a zoom
range to deal with the fact that injection/extraction pulses are
microseconds-scale inside a cycle that can be hundreds of milliseconds
long), dry-run/live hardware mirroring, and a timestamped changelog are
all functional end to end.

Editing the pulse table never touches the card on its own. Edits update
the data model and the diagram, nothing is written to hardware until the
user clicks "Push to card" and confirms the preview dialog. This is the
deliberate, safe-by-default behavior (the original app re-programmed on
every keystroke) and mirrors the LSTAR MPOD control GUI.

Intentionally left as follow-ups, just to keep this skeleton focused:
  - Add/remove channel row UI (core.PulseConfig.add_channel/remove_channel
    already support it)
  - A real toolbar-based zoom/pan instead of typed min/max
  - Popping the changelog out into its own window for long sessions
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib import colormaps

from core import (PulseConfig, PulseFormatError, load_pulses, save_pulses,
                  short_regions, MAX_CHANNELS)
from timing_card import TimingCard, connect_or_dry_run
from changelog import Changelog

# Anchor default resource paths to this file's directory, NOT the current
# working directory. This is what lets "open the folder in VS Code and hit
# Run" work from the repo root: without it, DEFAULT_PULSE_FILE would be
# resolved against wherever the process happens to be launched from.
SRC_DIR = Path(__file__).resolve().parent
PULSES_DIR = SRC_DIR / "pulses"          # all pulse-timing files live here
DEFAULT_PULSE_FILE = str(PULSES_DIR / "pulses3.txt")


class PulseControllerApp(ttk.Frame):
    def __init__(self, master: tk.Tk, config: PulseConfig, card: TimingCard,
                 pulse_file: str, changelog: Changelog, startup_warning: str | None = None):
        super().__init__(master)
        self.master = master
        self.config_ = config
        self.card = card
        self.pulse_file = pulse_file
        self.changelog = changelog
        self.multiplier = tk.DoubleVar(value=1.0)
        self.period_var = tk.StringVar(value=f"{config.period:.2f}")
        self.entries: dict[tuple[int, str], ttk.Entry] = {}
        self.end_labels: dict[int, ttk.Label] = {}

        # Snapshot of what is currently programmed on the card. None means
        # nothing has been pushed this session, so the card is running
        # whatever it was before the app started. `dirty` tracks whether the
        # in-memory config differs from that snapshot (unpushed edits),
        # `unsaved` tracks whether it differs from the file on disk.
        self.last_pushed: dict | None = None
        self.last_push_info: dict | None = None   # metadata for readback
        self.dirty = True
        self.unsaved = False

        self.pack(fill="both", expand=True)
        self._build_layout()
        self._refresh_view()

        if startup_warning:
            self.after(250, lambda: messagebox.showwarning("Dry-run mode", startup_warning))

    # ---------------------------------------------------------- layout ----

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_toolbar().grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=6)
        self._build_table().grid(row=1, column=0, sticky="ns", padx=8, pady=6)

        right = ttk.Frame(self)
        right.grid(row=1, column=1, sticky="nsew", padx=8, pady=6)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self._build_plot(right).grid(row=0, column=0, sticky="nsew")
        self._build_changelog_panel(right).grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def _build_toolbar(self) -> ttk.Frame:
        bar = ttk.Frame(self)
        ttk.Label(bar, text="Pulse file:").pack(side="left")
        self.file_label = ttk.Label(bar, text=self.pulse_file, width=44, relief="sunken", anchor="w")
        self.file_label.pack(side="left", padx=(4, 8))
        ttk.Button(bar, text="Load...", command=self._on_load).pack(side="left", padx=2)
        ttk.Button(bar, text="Save as...", command=self._on_save).pack(side="left", padx=2)

        mode = "LIVE, connected to hardware" if self.card.enabled else "DRY RUN, no hardware"
        color = "#1a7f37" if self.card.enabled else "#b35900"
        self.status_label = tk.Label(bar, text=mode, fg=color)
        self.status_label.pack(side="right", padx=8)

        # Push to card, the one place edits actually reach hardware. Opens a
        # confirmation dialog showing every change before anything is written.
        self.push_btn = tk.Button(bar, text="▶  PUSH TO CARD",
                                  bg="#b3261e", fg="white",
                                  font=("TkDefaultFont", 9, "bold"),
                                  relief="raised", borderwidth=2,
                                  command=self._on_push)
        self.push_btn.pack(side="right", padx=8, ipady=1)
        self.dirty_label = tk.Label(bar, text="", fg="#b35900",
                                    font=("TkDefaultFont", 9, "bold"))
        self.dirty_label.pack(side="right", padx=2)

        # Readback: query the board's run state and confirm the last push
        ttk.Button(bar, text="Readback", command=self._on_readback).pack(side="right", padx=2)
        return bar

    def _build_table(self) -> ttk.Frame:
        outer = ttk.Frame(self)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        for text, width in [("", 3), ("Ch", 4), ("Connection", 16), ("Inv", 4),
                            ("Start (us)", 18), ("Dur (us)", 12), ("End (us)", 10)]:
            ttk.Label(header, text=text, width=width, anchor="center",
                      font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=2)

        canvas = tk.Canvas(outer, height=420, width=560, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        rows_frame = ttk.Frame(canvas)
        rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")

        for row in self.config_.pulses.index:
            self._build_row(rows_frame, row)

        # Add-channel button sits at the bottom of the channel list (inside
        # the scroll area) so it reads as "add another row here"
        add_row = ttk.Frame(rows_frame)
        add_row.pack(fill="x", pady=(4, 2))
        ttk.Button(add_row, text="＋ Add channel",
                   command=self._on_add_channel).pack(side="left", padx=2)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        ttk.Label(footer, text="Step [us]:").pack(side="left")
        ttk.Button(footer, text="x0.1", width=4,
                   command=lambda: self._scale_multiplier(0.1)).pack(side="left", padx=2)
        ttk.Entry(footer, textvariable=self.multiplier, width=8, state="readonly").pack(side="left")
        ttk.Button(footer, text="x10", width=4,
                   command=lambda: self._scale_multiplier(10.0)).pack(side="left", padx=2)

        period_frame = ttk.Frame(outer)
        period_frame.pack(fill="x", pady=(4, 0))
        ttk.Label(period_frame, text="Period [us]:").pack(side="left")
        ttk.Button(period_frame, text="<", width=2,
                   command=lambda: self._increment_period(-1)).pack(side="left")
        period_entry = ttk.Entry(period_frame, textvariable=self.period_var, width=14)
        period_entry.pack(side="left", padx=4)
        period_entry.bind("<Return>", self._on_period_commit)
        period_entry.bind("<FocusOut>", self._on_period_commit)
        ttk.Button(period_frame, text=">", width=2,
                   command=lambda: self._increment_period(1)).pack(side="left")

        return outer

    def _build_row(self, parent: ttk.Frame, row: int) -> None:
        pulses = self.config_.pulses
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=1)

        channel = int(pulses.at[row, "Channel"])
        tk.Button(r, text="✕", width=2, fg="#b3261e",
                  font=("TkDefaultFont", 8, "bold"), relief="flat",
                  command=lambda c=channel: self._on_remove_channel(c)).pack(side="left", padx=2)
        ttk.Label(r, text=str(channel), width=4, anchor="center").pack(side="left", padx=2)
        connection = str(pulses.at[row, "Connection"]) if "Connection" in pulses.columns else ""
        ttk.Label(r, text=connection, width=20, anchor="center").pack(side="left", padx=2)

        invert_var = tk.BooleanVar(value=bool(pulses.at[row, "Invert"]))
        cb = ttk.Checkbutton(r, variable=invert_var,
                              command=lambda: self._on_invert_toggle(row, invert_var))
        cb.pack(side="left", padx=8)

        self._build_numeric_cell(r, row, "Start")
        self._build_numeric_cell(r, row, "Duration")

        end_label = ttk.Label(r, text=f"{pulses.at[row, 'End']:.2f}", width=10, anchor="center")
        end_label.pack(side="left", padx=2)
        self.end_labels[row] = end_label

    def _build_numeric_cell(self, parent: ttk.Frame, row: int, column: str) -> None:
        pulses = self.config_.pulses
        cell = ttk.Frame(parent)
        cell.pack(side="left", padx=2)

        ttk.Button(cell, text="<", width=2,
                   command=lambda: self._increment(row, column, -1)).pack(side="left")
        entry = ttk.Entry(cell, width=10, justify="right")
        entry.insert(0, f"{pulses.at[row, column]:.2f}")
        entry.pack(side="left")
        entry.bind("<Return>", lambda e: self._commit_entry(row, column))
        entry.bind("<FocusOut>", lambda e: self._commit_entry(row, column))
        ttk.Button(cell, text=">", width=2,
                   command=lambda: self._increment(row, column, 1)).pack(side="left")

        self.entries[(row, column)] = entry

    def _build_plot(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        zoom_bar = ttk.Frame(frame)
        zoom_bar.pack(fill="x")
        ttk.Label(zoom_bar, text="View [us]:").pack(side="left")
        self.zoom_min = tk.StringVar(value="0")
        self.zoom_max = tk.StringVar(value=f"{self.config_.period:.2f}")
        zmin = ttk.Entry(zoom_bar, textvariable=self.zoom_min, width=10)
        zmin.pack(side="left", padx=2)
        ttk.Label(zoom_bar, text="to").pack(side="left")
        zmax = ttk.Entry(zoom_bar, textvariable=self.zoom_max, width=10)
        zmax.pack(side="left", padx=2)
        zmin.bind("<Return>", lambda e: self._update_plot())
        zmax.bind("<Return>", lambda e: self._update_plot())
        ttk.Button(zoom_bar, text="−", width=2,
                   command=lambda: self._zoom(1.6)).pack(side="left", padx=(6, 1))
        ttk.Button(zoom_bar, text="＋", width=2,
                   command=lambda: self._zoom(1 / 1.6)).pack(side="left", padx=1)
        ttk.Button(zoom_bar, text="Fit pulses", command=self._fit_pulses).pack(side="left", padx=4)
        ttk.Button(zoom_bar, text="Full cycle", command=self._reset_zoom).pack(side="left", padx=2)
        ttk.Label(zoom_bar, text="(scroll to zoom)", foreground="#888").pack(side="left", padx=6)

        self.fig = Figure(figsize=(6, 3.2), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        # Mouse-wheel zoom centered on the cursor, the fastest way to move
        # between the microsecond-scale pulses and the full cycle.
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        return frame

    def _build_changelog_panel(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent)
        ttk.Label(frame, text="Changelog", font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        text_frame = ttk.Frame(frame)
        text_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(text_frame, height=8, state="disabled", wrap="none",
                                 font=("TkFixedFont", 9))
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self._refresh_changelog()
        return frame

    # --------------------------------------------------------- actions ----

    def _scale_multiplier(self, factor: float) -> None:
        self.multiplier.set(round(self.multiplier.get() * factor, 6))

    def _increment(self, row: int, column: str, direction: int) -> None:
        amount = direction * self.multiplier.get()
        old = float(self.config_.pulses.at[row, column])
        try:
            self.config_.increment(row, column, amount)
        except PulseFormatError as err:
            messagebox.showerror("Invalid value", str(err))
            return
        new = float(self.config_.pulses.at[row, column])
        channel = int(self.config_.pulses.at[row, "Channel"])
        self.changelog.record(f"EDIT channel {channel}: {column} {old:.2f} -> {new:.2f}")
        self._refresh_row(row)
        self._mark_dirty()
        self._refresh_view()

    def _commit_entry(self, row: int, column: str) -> None:
        entry = self.entries[(row, column)]
        old = float(self.config_.pulses.at[row, column])
        try:
            new = float(entry.get())
        except ValueError:
            self._refresh_row(row)
            return
        if new == old:
            return
        try:
            self.config_.set_value(row, column, new)
        except PulseFormatError as err:
            messagebox.showerror("Invalid value", str(err))
            self._refresh_row(row)
            return
        channel = int(self.config_.pulses.at[row, "Channel"])
        self.changelog.record(f"EDIT channel {channel}: {column} {old:.2f} -> {new:.2f}")
        self._refresh_row(row)
        self._mark_dirty()
        self._refresh_view()

    def _on_invert_toggle(self, row: int, var: tk.BooleanVar) -> None:
        old = bool(self.config_.pulses.at[row, "Invert"])
        new = bool(var.get())
        self.config_.set_value(row, "Invert", new)
        channel = int(self.config_.pulses.at[row, "Channel"])
        self.changelog.record(f"EDIT channel {channel}: Invert {old} -> {new}")
        self._mark_dirty()
        self._refresh_view()

    def _increment_period(self, direction: int) -> None:
        """Nudge the period by ±(current Step), mirroring the cell arrows."""
        old = self.config_.period
        try:
            self.config_.set_period(old + direction * self.multiplier.get())
        except PulseFormatError as err:
            messagebox.showerror("Invalid period", str(err))
            return
        new = self.config_.period
        self.period_var.set(f"{new:.2f}")
        self.changelog.record(f"EDIT period {old:.2f} -> {new:.2f} us")
        self.zoom_max.set(f"{new:.2f}")
        self._mark_dirty()
        self._refresh_view()

    def _on_period_commit(self, event=None) -> None:
        old = self.config_.period
        try:
            new = float(self.period_var.get())
        except ValueError:
            self.period_var.set(f"{old:.2f}")
            return
        if new == old:
            return
        try:
            self.config_.set_period(new)
        except PulseFormatError as err:
            messagebox.showerror("Invalid period", str(err))
            self.period_var.set(f"{old:.2f}")
            return
        self.changelog.record(f"EDIT period {old:.2f} -> {new:.2f} us")
        self.zoom_max.set(f"{new:.2f}")
        self._mark_dirty()
        self._refresh_view()

    def _refresh_row(self, row: int) -> None:
        for col in ("Start", "Duration"):
            entry = self.entries[(row, col)]
            entry.delete(0, "end")
            entry.insert(0, f"{self.config_.pulses.at[row, col]:.2f}")
        self.end_labels[row].configure(text=f"{self.config_.pulses.at[row, 'End']:.2f}")

    # --------------------------------------------- add / remove channels ----

    def _rebuild_layout(self) -> None:
        """Tear down and rebuild the whole layout after the channel set or
        period changes. The pulse-table rows are built from the dataframe
        index, so the simplest correct refresh is a full rebuild (same
        approach as loading a new file)."""
        for child in self.winfo_children():
            child.destroy()
        self.entries.clear()
        self.end_labels.clear()
        self.period_var.set(f"{self.config_.period:.2f}")
        self._build_layout()
        self._refresh_view()

    def _on_add_channel(self) -> None:
        used = set(self.config_.pulses["Channel"].astype(int))
        default = next((c for c in range(MAX_CHANNELS) if c not in used), None)
        if default is None:
            messagebox.showinfo("No free channels",
                                f"All {MAX_CHANNELS} channels are already in use.")
            return
        dlg = AddChannelDialog(self.master, used, default)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        channel, connection, start, duration, invert = dlg.result
        try:
            self.config_.add_channel(channel, connection=connection, start=start,
                                     duration=duration, invert=invert)
        except PulseFormatError as err:
            messagebox.showerror("Cannot add channel", str(err))
            return
        self.changelog.record(
            f"ADD channel {channel} ({connection or 'unnamed'}): "
            f"Start {start:.2f}, Dur {duration:.2f}, Invert {invert}")
        self._mark_dirty()
        self._rebuild_layout()

    def _on_remove_channel(self, channel: int) -> None:
        connection = ""
        match = self.config_.pulses[self.config_.pulses["Channel"] == channel]
        if not match.empty and "Connection" in match.columns:
            connection = str(match.iloc[0]["Connection"])
        if not messagebox.askyesno(
                "Remove channel",
                f"Remove channel {channel}"
                + (f" ({connection})" if connection else "") + "?"):
            return
        try:
            self.config_.remove_channel(channel)
        except PulseFormatError as err:
            messagebox.showerror("Cannot remove channel", str(err))
            return
        self.changelog.record(f"REMOVE channel {channel} ({connection or 'unnamed'})")
        self._mark_dirty()
        self._rebuild_layout()

    def _reset_zoom(self) -> None:
        self.zoom_min.set("0")
        self.zoom_max.set(f"{self.config_.period:.2f}")
        self._update_plot()

    def _current_view(self) -> tuple[float, float]:
        try:
            return float(self.zoom_min.get()), float(self.zoom_max.get())
        except ValueError:
            return 0.0, self.config_.period

    def _set_view(self, xmin: float, xmax: float) -> None:
        # Clamp to the cycle and keep a sane minimum width so zooming in
        # never collapses the axis to zero.
        period = self.config_.period
        xmin = max(0.0, min(xmin, period))
        xmax = min(period, max(xmax, xmin + 1e-6))
        self.zoom_min.set(f"{xmin:.2f}")
        self.zoom_max.set(f"{xmax:.2f}")
        self._update_plot()

    def _zoom(self, factor: float, center: float | None = None) -> None:
        """Scale the view width by `factor` about `center` (default: midpoint)."""
        xmin, xmax = self._current_view()
        if center is None:
            center = 0.5 * (xmin + xmax)
        width = (xmax - xmin) * factor
        self._set_view(center - width / 2, center + width / 2)

    def _on_scroll(self, event) -> None:
        if event.xdata is None:
            return
        # event.step > 0 scrolls up = zoom in, < 0 = zoom out.
        factor = (1 / 1.3) if event.step > 0 else 1.3
        self._zoom(factor, center=event.xdata)

    def _fit_pulses(self) -> None:
        """Zoom to the window that actually contains pulse activity, with a
        small margin. Useful because injection/extraction pulses are tiny
        slivers inside a cycle that can be hundreds of milliseconds long."""
        pulses = self.config_.pulses
        if pulses.empty:
            self._reset_zoom()
            return
        lo = float(pulses["Start"].min())
        hi = float(pulses["End"].max())
        margin = max((hi - lo) * 0.05, 1.0)
        self._set_view(lo - margin, hi + margin)

    def _refresh_changelog(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in self.changelog.recent(200):
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # --------------------------------------------------- file handling ----

    def _on_load(self) -> None:
        if self.unsaved and not messagebox.askyesno(
                "Discard unsaved edits?",
                "The current pulse file has unsaved edits.\n"
                "Loading another file will discard them. Continue?"):
            return
        path = filedialog.askopenfilename(initialdir=self._pulse_dir(),
                                          filetypes=[("Pulse files", "*.txt")])
        if not path:
            return
        try:
            new_config = load_pulses(path)
        except Exception as err:
            messagebox.showerror("Load failed", str(err))
            return
        self.config_ = new_config
        self.pulse_file = path
        self.file_label.configure(text=path)
        self.changelog.record(f"LOAD {path}")

        # A freshly loaded file has not been programmed onto the card yet
        # (dirty), but it matches its file on disk (not unsaved).
        self.last_pushed = None
        for child in self.winfo_children():
            child.destroy()
        self.entries.clear()
        self.end_labels.clear()
        self.period_var.set(f"{self.config_.period:.2f}")
        self._build_layout()          # (re)creates dirty_label
        self.dirty = True
        self.unsaved = False
        self.dirty_label.configure(text="● unpushed edits")
        self._refresh_view()

    def _pulse_dir(self) -> str:
        """Default directory for the load/save dialogs: the current pulse
        file's folder if it has one, else the shared pulses/ folder."""
        parent = Path(self.pulse_file).resolve().parent
        return str(parent if parent.exists() else PULSES_DIR)

    def _on_save(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                             initialdir=self._pulse_dir(),
                                             filetypes=[("Pulse files", "*.txt")])
        if not path:
            return
        try:
            save_pulses(self.config_, path)
        except Exception as err:
            messagebox.showerror("Save failed", str(err))
            return
        self.pulse_file = path
        self.file_label.configure(text=path)
        self.changelog.record(f"SAVE {path}")
        self._mark_saved()
        self._refresh_changelog()

    # ------------------------------------------------------- push to card ----

    def _snapshot(self) -> dict:
        """Serializable picture of the current config, for diffing on push."""
        pulses = self.config_.pulses
        return {
            "period": float(self.config_.period),
            "channels": {
                int(p.Channel): {
                    "Connection": str(getattr(p, "Connection", "")),
                    "Start": float(p.Start),
                    "Duration": float(p.Duration),
                    "Invert": bool(p.Invert),
                }
                for p in pulses.itertuples(index=False)
            },
        }

    def _diff_since_push(self, current: dict) -> list[str]:
        """Human-readable lines describing what changed since the last push."""
        if self.last_pushed is None:
            n = len(current["channels"])
            return [f"First push this session, programming all {n} channel(s)."]

        prev = self.last_pushed
        lines: list[str] = []
        if abs(current["period"] - prev["period"]) > 1e-9:
            lines.append(f"Period: {prev['period']:.2f} -> {current['period']:.2f} us")

        cur_ch, prev_ch = current["channels"], prev["channels"]
        for ch in sorted(cur_ch):
            new = cur_ch[ch]
            if ch not in prev_ch:
                lines.append(f"Channel {ch} ({new['Connection']}): new channel")
                continue
            old = prev_ch[ch]
            for field in ("Start", "Duration", "Invert"):
                if new[field] != old[field]:
                    if field == "Invert":
                        lines.append(f"Channel {ch} ({new['Connection']}): "
                                     f"Invert {old[field]} -> {new[field]}")
                    else:
                        lines.append(f"Channel {ch} ({new['Connection']}): "
                                     f"{field} {old[field]:.2f} -> {new[field]:.2f}")
        for ch in sorted(prev_ch):
            if ch not in cur_ch:
                lines.append(f"Channel {ch}: removed")

        return lines or ["No changes since last push."]

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._mark_unsaved()
        if hasattr(self, "dirty_label"):
            self.dirty_label.configure(text="● unpushed edits")

    def _mark_clean(self) -> None:
        self.dirty = False
        if hasattr(self, "dirty_label"):
            self.dirty_label.configure(text="")

    def _mark_unsaved(self) -> None:
        self.unsaved = True

    def _mark_saved(self) -> None:
        self.unsaved = False

    def _on_push(self) -> None:
        try:
            regions = self.config_.regions()
        except PulseFormatError as err:
            messagebox.showerror("Invalid configuration", str(err))
            return

        current = self._snapshot()
        changes = self._diff_since_push(current)

        # Warn about regions the card physically cannot represent (edges
        # closer together than the minimum instruction length).
        min_us = self.card.min_instruction_us
        too_short = short_regions(regions, min_us)

        dlg = PushDialog(self.master, self.config_, regions, changes,
                         live=self.card.enabled,
                         too_short=too_short, min_us=min_us)
        self.wait_window(dlg)
        if not dlg.confirmed:
            self.changelog.record("PUSH cancelled")
            return

        mode = "live" if self.card.enabled else "dry-run"
        self.changelog.record(
            f"PUSH requested: {len(current['channels'])} channels, "
            f"period={current['period']:.2f} us, {len(regions)} regions, mode={mode}")
        try:
            self.card.apply(regions, verbose=False)
        except Exception as err:  # never let a hardware fault kill the GUI
            self.changelog.record(f"PUSH FAILED: {err}")
            messagebox.showerror("Push failed", str(err))
            self._refresh_changelog()
            return

        self.changelog.record(f"PUSH result: programmed {len(regions)} regions ({mode})")
        self.last_pushed = current
        self.last_push_info = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "channels": len(current["channels"]),
            "period": current["period"],
            "regions": len(regions),
            "mode": mode,
        }
        self._mark_clean()
        self._refresh_changelog()

    def _on_readback(self) -> None:
        """Query the board and show its run state alongside the app's record
        of the last push. Confirms the card is on and that a push took."""
        try:
            status = self.card.read_status()
        except Exception as err:
            self.changelog.record(f"READBACK failed: {err}")
            messagebox.showerror("Readback failed", str(err))
            self._refresh_changelog()
            return

        if status is None:
            self.changelog.record("READBACK: dry-run, no hardware")
        else:
            self.changelog.record(
                f"READBACK: status={'/'.join(status['flags']) or 'unknown'} "
                f"(0x{status['raw']:x})")
        dlg = ReadbackDialog(self.master, live=self.card.enabled, status=status,
                             last_push_info=self.last_push_info, dirty=self.dirty)
        self._refresh_changelog()
        self.wait_window(dlg)

    # ----------------------------------------------------- view + plot ----

    def _refresh_view(self) -> None:
        """Redraw the plot and changelog. Doesn't touches the card, that only
        happens on an explicit push."""
        self._update_plot()
        self._refresh_changelog()

    def _update_plot(self) -> None:
        try:
            xmin = float(self.zoom_min.get())
            xmax = float(self.zoom_max.get())
        except ValueError:
            xmin, xmax = 0.0, self.config_.period

        self.ax.clear()
        pulses = self.config_.pulses
        period = self.config_.period
        height = 0.35
        channels = pulses["Channel"].astype(int).tolist()
        cmap = colormaps["tab20"]
        color_by_channel = {c: cmap(i % cmap.N) for i, c in enumerate(sorted(channels))}

        for _, pulse in pulses.iterrows():
            channel = int(pulse["Channel"])
            start, end = float(pulse["Start"]), float(pulse["End"])
            invert = bool(pulse["Invert"])
            color = color_by_channel[channel]
            y_low, y_high = channel, channel + height
            y_off, y_on = (y_high, y_low) if invert else (y_low, y_high)
            x = [0, start, start, end, end, period]
            y = [y_off, y_off, y_on, y_on, y_off, y_off]
            if invert:
                self.ax.fill_between([start, end], y_on, y_off, color=color, alpha=0.2, step="post")
            else:
                self.ax.fill_between([0, start], y_on, y_off, color=color, alpha=0.2, step="post")
                self.ax.fill_between([end, period], y_on, y_off, color=color, alpha=0.2, step="post")
            self.ax.plot(x, y, drawstyle="steps-post", color=color)

        self.ax.set_xlim(xmin, xmax)
        self.ax.set_ylim(max(channels) + 1.0, min(channels) - 0.5)
        self.ax.set_yticks(channels)
        self.ax.set_ylabel("Channel")
        self.ax.set_xlabel("Time in cycle [us]")
        self.ax.grid(True, axis="x", alpha=0.3)
        self.fig.tight_layout()
        self.canvas.draw()


class AddChannelDialog(tk.Toplevel):
    """Modal form for adding a new channel. Validation of the channel number
    against the in-use set and the [0, MAX_CHANNELS) range happens here,
    core.PulseConfig.add_channel re-validates everything else. Sets
    ``self.result`` to (channel, connection, start, duration, invert) or
    leaves it None on cancel."""

    def __init__(self, parent: tk.Widget, used: set[int], default_channel: int):
        super().__init__(parent)
        self.title("Add channel")
        self.resizable(False, False)
        self.grab_set()
        self.result: tuple | None = None
        self._used = used

        self._channel = tk.StringVar(value=str(default_channel))
        self._connection = tk.StringVar(value="")
        self._start = tk.StringVar(value="0.00")
        self._duration = tk.StringVar(value="0.00")
        self._invert = tk.BooleanVar(value=True)

        form = ttk.Frame(self)
        form.pack(padx=14, pady=12)
        rows = [
            ("Channel (0–%d):" % (MAX_CHANNELS - 1), self._channel),
            ("Connection (no spaces):", self._connection),
            ("Start [us]:", self._start),
            ("Duration [us]:", self._duration),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            ttk.Entry(form, textvariable=var, width=24).grid(row=i, column=1, pady=3)
        ttk.Checkbutton(form, text="Invert (idle high)",
                        variable=self._invert).grid(row=len(rows), column=1,
                                                    sticky="w", pady=(4, 0))

        bf = ttk.Frame(self)
        bf.pack(pady=(0, 12))
        ttk.Button(bf, text="Add", command=self._ok).pack(side="left", padx=8)
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=8)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())

    def _ok(self) -> None:
        try:
            channel = int(self._channel.get())
        except ValueError:
            messagebox.showerror("Invalid channel", "Channel must be a whole number.", parent=self)
            return
        if not (0 <= channel < MAX_CHANNELS):
            messagebox.showerror("Invalid channel",
                                 f"Channel must be in 0–{MAX_CHANNELS - 1}.", parent=self)
            return
        if channel in self._used:
            messagebox.showerror("Invalid channel",
                                 f"Channel {channel} is already in use.", parent=self)
            return
        connection = self._connection.get().strip().replace(" ", "_")
        try:
            start = float(self._start.get())
            duration = float(self._duration.get())
        except ValueError:
            messagebox.showerror("Invalid value",
                                 "Start and Duration must be numbers.", parent=self)
            return
        self.result = (channel, connection, start, duration, self._invert.get())
        self.destroy()


class ReadbackDialog(tk.Toplevel):
    """Shows the board's live run state next to the app's record of the last
    push. The PulseBlaster can report whether it is running, but not which 
    pattern is loaded. That half comes from what this app last pushed."""

    def __init__(self, parent: tk.Widget, live: bool, status: dict | None,
                 last_push_info: dict | None, dirty: bool):
        super().__init__(parent)
        self.title("Card readback")
        self.resizable(False, False)
        self.grab_set()

        ttk.Label(self, text="Card readback",
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 6))

        # ── Live hardware state ──────────────────────────────────────────
        ttk.Label(self, text="Hardware (live query):",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", padx=12)
        if not live or status is None:
            tk.Label(self, text="   DRY RUN - no physical card connected.\n"
                                "   Nothing is programmed on hardware.",
                     fg="#b35900", justify="left").pack(anchor="w", padx=12, pady=(0, 6))
        else:
            running = status["running"]
            state = "/".join(status["flags"]).upper() or "UNKNOWN"
            tk.Label(self, text=f"   Run state: {state}   (0x{status['raw']:x})",
                     fg="#1a7f37" if running else "#b3261e",
                     font=("TkDefaultFont", 10, "bold"),
                     justify="left").pack(anchor="w", padx=12)
            if status.get("message"):
                ttk.Label(self, text=f"   Board says: {status['message']}"
                          ).pack(anchor="w", padx=12)
            if not running:
                tk.Label(self, text="   ⚠ Board is not running, no pulses are being output.",
                         fg="#b3261e", justify="left").pack(anchor="w", padx=12)
            ttk.Label(self, text="   (The card cannot report its loaded pulse "
                                 "pattern, only its run state.)",
                      foreground="#888").pack(anchor="w", padx=12, pady=(0, 6))

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=4)

        # ── App's record of the last push ────────────────────────────────
        ttk.Label(self, text="Last push from this app:",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", padx=12)
        if last_push_info is None:
            tk.Label(self,
                     text="   Nothing pushed yet this session.\n"
                          "   The card is running whatever it held before the app started.",
                     fg="#b35900", justify="left").pack(anchor="w", padx=12, pady=(0, 6))
        else:
            info = last_push_info
            ttk.Label(self,
                      text=f"   {info['time']}\n"
                           f"   {info['channels']} channels, "
                           f"period {info['period']:.2f} us, "
                           f"{info['regions']} regions ({info['mode']})",
                      justify="left").pack(anchor="w", padx=12)
            if dirty:
                tk.Label(self,
                         text="   ⚠ There are unpushed edits. The card does NOT "
                              "reflect the current table.",
                         fg="#b3261e", justify="left").pack(anchor="w", padx=12)
            ttk.Label(self, text="").pack(pady=1)

        ttk.Button(self, text="Close", command=self.destroy).pack(pady=(4, 12))
        self.bind("<Escape>", lambda e: self.destroy())


class PushDialog(tk.Toplevel):
    """Modal preview shown before anything is written to the timing card.

    Lists every change since the last push at the top, then the full pulse
    table that will be programmed, so the operator confirms against the
    complete resulting state, not just the diff. Mirrors the LSTAR MPOD
    PushDialog. Sets ``self.confirmed`` for the caller to check after
    ``wait_window``
    """

    def __init__(self, parent: tk.Widget, config: PulseConfig, regions,
                 changes: list[str], live: bool,
                 too_short=None, min_us: float = 0.0):
        super().__init__(parent)
        self.title("Confirm push to card")
        self.resizable(False, False)
        self.grab_set()
        self.confirmed = False

        dest = ("LIVE, writing to the PulseBlaster"
                if live else "DRY RUN, nothing will be written to hardware")
        dest_color = "#1a7f37" if live else "#b35900"

        ttk.Label(self, text="Confirm push to card",
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(self, text=dest, fg=dest_color,
                 font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=12)
        ttk.Label(self,
                  text=f"Period: {config.period:.2f} us     "
                       f"Channels: {len(config.pulses)}     "
                       f"Regions to program: {len(regions)}",
                  ).pack(anchor="w", padx=12, pady=(2, 6))

        # ── Hardware-limit warning ───────────────────────────────────────
        n_short = 0 if too_short is None else len(too_short)
        if n_short:
            worst = float(too_short["Duration"].min())
            tk.Label(
                self,
                text=(f"⚠  {n_short} region(s) are shorter than the card "
                      f"minimum of {min_us:.3f} us (shortest {worst:.4f} us).\n"
                      f"    The card cannot represent these; the real output "
                      f"will differ from the diagram."),
                fg="#b3261e", justify="left",
                font=("TkDefaultFont", 9, "bold"),
            ).pack(anchor="w", padx=12, pady=(0, 6))

        # ── Changes since last push ──────────────────────────────────────
        ttk.Label(self, text="Changes since last push:",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", padx=12)
        chg = tk.Text(self, width=64, height=min(len(changes) + 1, 10),
                      font=("TkFixedFont", 9), relief="flat", wrap="none")
        chg.pack(fill="x", padx=12, pady=(2, 6))
        for line in changes:
            chg.insert("end", f"  {line}\n")
        chg.configure(state="disabled")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=4)

        # ── Full resulting table ─────────────────────────────────────────
        ttk.Label(self, text="Full configuration to be programmed:",
                  font=("TkDefaultFont", 9, "bold")).pack(anchor="w", padx=12)
        pulses = config.pulses
        table = tk.Text(self, width=64, height=min(len(pulses) + 2, 16),
                        font=("TkFixedFont", 9), relief="flat", wrap="none")
        table.pack(fill="x", padx=12, pady=(2, 6))
        table.insert("end", f"{'Ch':>3}  {'Connection':<22} {'Start':>12} "
                            f"{'Dur':>10} {'End':>12} {'Inv':>5}\n")
        table.insert("end", "─" * 70 + "\n")
        for p in pulses.itertuples(index=False):
            conn = str(getattr(p, "Connection", ""))[:22]
            table.insert("end",
                         f"{int(p.Channel):>3}  {conn:<22} {float(p.Start):>12.2f} "
                         f"{float(p.Duration):>10.2f} {float(p.End):>12.2f} "
                         f"{str(bool(p.Invert)):>5}\n")
        table.configure(state="disabled")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=4)

        # ── Buttons ──────────────────────────────────────────────────────
        bf = ttk.Frame(self)
        bf.pack(pady=(0, 12))
        label = "Run dry-run (log only)" if not live else "✓  Confirm push"
        tk.Button(bf, text=label,
                  bg="#b35900" if not live else "#b3261e", fg="white",
                  font=("TkDefaultFont", 10, "bold"),
                  command=self._confirm).pack(side="left", padx=8, ipadx=6, ipady=2)
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=8)

        self.bind("<Escape>", lambda e: self.destroy())

    def _confirm(self) -> None:
        self.confirmed = True
        self.destroy()


def run(pulse_file: str = DEFAULT_PULSE_FILE) -> None:
    config = load_pulses(pulse_file)
    card, startup_warning = connect_or_dry_run()
    changelog = Changelog(Path(pulse_file).with_suffix(".changelog.log"))
    mode = "live" if card.enabled else "dry-run"
    changelog.record(f"SESSION START: pulse_file={pulse_file}, mode={mode}")

    root = tk.Tk()
    root.title("TAMUTRAP Pulse Controller")
    root.geometry("1280x720")
    app = PulseControllerApp(root, config, card, pulse_file, changelog, startup_warning)

    def on_close():
        if app.unsaved and not messagebox.askyesno(
                "Quit with unsaved edits?",
                "There are unsaved edits to the pulse file.\n"
                "Quit anyway? (The changelog is already saved.)"):
            return
        card.stop()
        card.close()
        changelog.record("SESSION END")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    run()
