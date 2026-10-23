import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Uvicorn loop factory: psycopg async needs a selector loop on Windows."""
    return asyncio.SelectorEventLoop()
