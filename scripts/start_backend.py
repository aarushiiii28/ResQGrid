"""Start the API (works on Windows, where psycopg needs a selector event loop)."""

import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8010
    uvicorn.run(
        "app.main:app",
        app_dir=str(ROOT / "backend"),
        port=port,
        loop="app.loop:selector_loop_factory",
    )
