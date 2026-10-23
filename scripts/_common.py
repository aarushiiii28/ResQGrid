import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app  # noqa: E402,F401  (sets the Windows event-loop policy)
from app.core.config import get_settings  # noqa: E402
from app.db.pg import Store  # noqa: E402
from app.db.seed import seed  # noqa: E402
from app.services.context import Ctx  # noqa: E402
from app.websockets.manager import EventHub  # noqa: E402


async def run(reset: bool) -> dict:
    s = get_settings()
    store = Store(s.database_url)
    await store.open()
    try:
        ctx = Ctx(settings=s, store=store, hub=EventHub())
        if reset:
            await store.truncate_all()
        elif await ctx.vehicle_repo.list():
            return {"skipped": "already seeded; use reset_demo.py to wipe"}
        out = await seed(ctx)
        await store.put("meta", "seeded", {"id": "seeded"})
        return out
    finally:
        await store.close()


def main(reset: bool) -> None:
    print(asyncio.run(run(reset)))
