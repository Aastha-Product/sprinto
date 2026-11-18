"""Vercel function: POST /api/run --- see src/brendo/web.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brendo.web import make_handler  # noqa: E402

handler = make_handler("run")
