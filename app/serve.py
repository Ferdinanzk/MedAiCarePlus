"""Run the app on two listeners that share one process, one lifespan and one monitor registry.

:8000 is public (browser, LINE webhook through the tunnel). :8001 is the robot
device API, reachable only on the compose network; app/main.py rejects device
routes and device tokens on the public port.
"""

import asyncio
import contextlib

import uvicorn

from app.config import DEVICE_PORT, PUBLIC_PORT
from app.main import app


async def main() -> None:
    public = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PUBLIC_PORT, lifespan="on"))
    device = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=DEVICE_PORT, lifespan="off"))
    # Signal handling belongs to the public server (it owns the lifespan); the
    # device server must not replace its SIGTERM/SIGINT handlers.
    setattr(device, "capture_signals", contextlib.nullcontext)
    public_task = asyncio.create_task(public.serve())
    while not public.started and not public_task.done():
        await asyncio.sleep(.05)
    device_task = asyncio.create_task(device.serve())
    await public_task
    device.should_exit = True
    await device_task


if __name__ == "__main__":
    asyncio.run(main())
