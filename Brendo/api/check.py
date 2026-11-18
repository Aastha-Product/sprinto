"""Vercel function: POST /api/check --- see src/brendo/web.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brendo.web import ApiHandler  # noqa: E402


class handler(ApiHandler):  # noqa: N801 --- Vercel looks for this exact name
    action = "check"
