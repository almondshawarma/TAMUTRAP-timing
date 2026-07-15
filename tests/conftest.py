"""Put ``src/`` on the import path so tests can ``import core`` etc.

Keeps the tests runnable with a bare ``pytest`` from the repo root without
installing the project as a package.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
