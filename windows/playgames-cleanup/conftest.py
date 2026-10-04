"""Puts this folder on sys.path so `pytest windows/playgames-cleanup/tests` works from
the repo root as well as from here. The package is not installed into the dev venv."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
