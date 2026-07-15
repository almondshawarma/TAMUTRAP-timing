"""Thin, dry-run-safe wrapper around the SpinCore SpinAPI / PulseBlaster
driver.

This module never lets an ImportError (or the NameError that happens when
spinapi.py imports fine but its underlying DLL load silently fails)
escape to the rest of the app. If real hardware isn't available, every
TimingCard method becomes a safe, printed no-op. The rest of the program
runs identically whether or not a card is physically attached -- the
``enabled`` flag here is the one place that decision gets made.

NOTE: spinapi.py is SpinCore Technologies' ctypes wrapper around
spinapi64.dll. Both are vendored in src/ (SpinCore ships them under a
permissive zlib-style license that allows redistribution), so a checkout
on the lab's card machine is import-and-go. On any machine without the
board, the import fails and this module runs dry-run automatically.
"""
from __future__ import annotations

import pandas as pd

DEFAULT_CORE_CLOCK_MHZ = 100.0

# PulseBlaster run-status bits, as returned by pb_read_status(). These match
# the STATUS_* constants in the vendor spinapi.py.
STATUS_FLAGS = {1: "stopped", 2: "reset", 4: "running", 8: "waiting"}


class TimingCard:
    """
    Parameters
    ----------
    enabled:
        If False, every method is a dry-run no-op and spinapi is never
        imported.
    board_num:
        Board index passed to SpinAPI when enabled.
    core_clock_mhz:
        PulseBlaster core clock, in MHz.
    """

    def __init__(self, enabled: bool = False, board_num: int = 0,
                 core_clock_mhz: float = DEFAULT_CORE_CLOCK_MHZ):
        self.enabled = bool(enabled)
        self.board_num = int(board_num)
        self.core_clock_mhz = float(core_clock_mhz)
        self._spinapi = None
        self._initialized = False

    # A PulseBlaster instruction must be at least 5 core-clock cycles long.
    # At the default 100 MHz that is 0.05 us. Anything shorter cannot be
    # programmed faithfully.
    MIN_INSTRUCTION_CYCLES = 5

    @property
    def min_instruction_us(self) -> float:
        """Shortest region duration (microseconds) the card can represent."""
        return self.MIN_INSTRUCTION_CYCLES / self.core_clock_mhz

    def _api(self):
        if not self.enabled:
            return None
        if self._spinapi is None:
            try:
                import spinapi  # type: ignore
            except Exception as exc:
                # Catches both "module not found" (ImportError) and
                # "module imported but its top-level DLL load failed and
                # left the `spinapi` name unbound" (NameError).
                raise RuntimeError(
                    "spinapi driver unavailable. Use TimingCard(enabled=False) "
                    "for dry runs."
                ) from exc
            self._spinapi = spinapi
        return self._spinapi

    def init(self) -> None:
        api = self._api()
        if api is None:
            print("TimingCard dry run: init skipped.")
            return

        version = api.pb_get_version()
        board_count = api.pb_count_boards()
        print(f"SpinAPI Library -V {version}")
        print(f"Found {board_count} board(s); selecting board {self.board_num}")

        if board_count <= self.board_num:
            raise RuntimeError(f"Cannot find board {self.board_num}. Found {board_count} board(s).")

        api.pb_select_board(self.board_num)
        if api.pb_init() != 0:
            raise RuntimeError(f"Error initializing board: {api.pb_get_error()}")

        api.pb_core_clock(self.core_clock_mhz)
        self._initialized = True

    def program(self, regions: pd.DataFrame, *, verbose: bool = True) -> None:
        """Program the card from a regions dataframe (see core.create_regions)."""
        regions = regions.reset_index(drop=True)
        if verbose:
            print("Programming timing regions:")
            print(regions.to_string(index=False))

        api = self._api()
        if api is None:
            print("TimingCard dry run: program skipped.")
            return

        if not self._initialized:
            self.init()

        api.pb_start_programming(api.PULSE_PROGRAM)
        start_instruction = api.pb_inst_pbonly(
            int(regions.loc[0, "State"]), api.Inst.CONTINUE, 0,
            float(regions.loc[0, "Duration"]) * api.us,
        )
        for i in range(1, len(regions) - 1):
            api.pb_inst_pbonly(
                int(regions.loc[i, "State"]), api.Inst.CONTINUE, 0,
                float(regions.loc[i, "Duration"]) * api.us,
            )
        last = len(regions) - 1
        api.pb_inst_pbonly(
            int(regions.loc[last, "State"]), api.Inst.BRANCH, start_instruction,
            float(regions.loc[last, "Duration"]) * api.us,
        )
        api.pb_stop_programming()

    def apply(self, regions: pd.DataFrame, *, verbose: bool = False) -> None:
        """Load a program and (re)start the board so it actually runs.

        Reprogramming a running PulseBlaster is done stop -> program ->
        start: ``pb_start_programming`` alone only loads instructions, it
        does not begin (or restart) execution. Doing the full cycle here is
        what makes "Push to card" take effect on live hardware. Every step
        is a no-op in dry-run mode.
        """
        self.stop()
        self.program(regions, verbose=verbose)
        self.start()

    def read_status(self) -> dict | None:
        """Query the board's run status. Returns None in dry-run mode.

        IMPORTANT: the PulseBlaster instruction memory is write-only -- there
        is no API to read the programmed pulse pattern back off the chip.
        What this reports is the *run state* (running / stopped / waiting /
        reset) plus the vendor status string, which is enough to confirm the
        board is powered, responding, and executing a program after a push.
        To know *what* pattern is loaded, compare against the app's own
        record of the last push.
        """
        api = self._api()
        if api is None:
            return None
        if not self._initialized:
            self.init()
        raw = int(api.pb_read_status())
        flags = [name for bit, name in STATUS_FLAGS.items() if raw & bit]
        message = ""
        try:
            message = api.pb_status_message()
        except Exception:
            pass  # status message is a nicety, never worth failing readback
        return {
            "raw": raw,
            "flags": flags,
            "message": message,
            "running": bool(raw & 4),
        }

    def start(self) -> None:
        api = self._api()
        if api is None:
            print("TimingCard dry run: start skipped.")
            return
        if not self._initialized:
            self.init()
        api.pb_start()

    def stop(self) -> None:
        api = self._api()
        if api is None:
            print("TimingCard dry run: stop skipped.")
            return
        api.pb_stop()

    def close(self) -> None:
        api = self._api()
        if api is None:
            print("TimingCard dry run: close skipped.")
            return
        api.pb_close()
        self._initialized = False


def connect_or_dry_run(board_num: int = 0) -> tuple[TimingCard, str | None]:
    """Try to connect to real hardware; fall back to a dry-run card on failure."""
    card = TimingCard(enabled=True, board_num=board_num)
    try:
        card.init()
        print("Timing card connected successfully.")
        return card, None
    except Exception as err:
        message = (
            "Timing card unavailable.\n\n"
            "Continuing in dry-run mode.\n"
            "You can still edit values and view the pulse diagram.\n\n"
            f"Error:\n{err}"
        )
        print("WARNING:", message)
        return TimingCard(enabled=False), message
