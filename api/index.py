import sys
from pathlib import Path

# Make sure the project root (with app/) is importable from inside api/
sys.path.append(str(Path(__file__).parent.parent))

from app.main import app  # noqa: E402

# Vercel's Python runtime looks for a variable called `app` (ASGI-compatible)
