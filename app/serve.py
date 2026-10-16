"""Run the app on two listeners that share one process, one lifespan and one monitor registry.

:8000 is public (browser, LINE webhook through the tunnel). :8001 is the robot
device API, reachable only on the compose network; app/main.py rejects device
routes and device tokens on the public port.
"""

import asyncio
import contextlib
import logging
import os
import sys

import uvicorn

from app.config import DEVICE_PORT, PUBLIC_PORT
from app.main import app

log = logging.getLogger("app.startup")


async def main() -> None:
    public = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PUBLIC_PORT, lifespan="on"))
    device = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=DEVICE_PORT, lifespan="off"))
    # Signal handling belongs to the public server (it owns the lifespan); the
    # device server must not replace its SIGTERM/SIGINT handlers.
    setattr(device, "capture_signals", contextlib.nullcontext)
    public_task = asyncio.create_task(public.serve())
    while not public.started and not public_task.done():
        await asyncio.sleep(.05)
    if public_task.done() and not public.started:
        # The lifespan failed (uvicorn logs "Application startup failed"): say so, and exit non-zero so the container's
        # restart policy and `docker ps` show a failure instead of a clean exit with no device port
        log.error("[STARTUP] FAILED: the application did not start (see the error above); exiting")
        await public_task
        return 1
    device_task = asyncio.create_task(device.serve())
    log.info("[STARTUP] device_api: OK listening on 0.0.0.0:%d in the container; the host maps DEVICE_BIND=%s:%d to it "
             "(scripts/medcare-boot.ps1 checks that the host port is really bound)", DEVICE_PORT,
             os.getenv("DEVICE_BIND", "127.0.0.1"), DEVICE_PORT)
    await public_task
    device.should_exit = True
    await device_task
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
