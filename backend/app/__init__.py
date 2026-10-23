import sys
import warnings

if sys.platform == "win32":
    # psycopg's async mode cannot run on the Windows ProactorEventLoop.
    import asyncio

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
