"""Vercel function: POST /api/approve --- see src/brendo/web.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brendo.web import make_handler  # noqa: E402

# Vercel's Python runtime only treats a file as a function if it can SEE a top-level
# `class handler(...)` (or `app`); an assignment like `handler = make_handler(...)` is
# skipped, and the build then fails with "unused_function ... api/*.py".
class handler(make_handler("approve")):
    pass
