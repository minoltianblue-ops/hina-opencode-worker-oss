"""Test bootstrap: make src/hina_opencode_worker importable without install.

Run: python3 -m unittest discover -s tests -v   (from the repo root)
"""
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
