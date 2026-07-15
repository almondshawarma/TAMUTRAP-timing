"""A timestamped audit trail of every action taken on a pulse configuration.

Appends one plain-text line per action to a ``.log`` file next to the pulse
file, and keeps an in-memory copy the GUI can display. The format simple
and greppable.

    [YYYY-MM-DD HH:MM:SS] [username] MESSAGE

"""
from __future__ import annotations

import getpass
from datetime import datetime
from pathlib import Path


class Changelog:
    """Append-only text log of edits, loads, saves, and card pushes."""

    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self.lines: list[str] = []
        if self.log_path.exists():
            self._load_existing()

    def _load_existing(self) -> None:
        with open(self.log_path, "r", encoding="utf-8") as f:
            self.lines = [line.rstrip("\n") for line in f if line.strip()]

    def record(self, message: str) -> str:
        """Append one timestamped, user-stamped line and return."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{getpass.getuser()}] {message}"
        self.lines.append(line)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # a read-only log directory must never take down the GUI
        return line

    def recent(self, n: int = 200) -> list[str]:
        return self.lines[-n:]
