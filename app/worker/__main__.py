from __future__ import annotations

import asyncio
import signal

from app.config import get_settings
from app.db import create_engine
from app.logging import configure_logging
from app.worker.loop import run


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings, pool_size=5)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(settings, engine, stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
