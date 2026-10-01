"""Minute job: task lease expiry, task expiry, robot-offline notices."""

from app.services import reachy_tasks


async def run_reachy_task_maintenance() -> None:
    await reachy_tasks.maintenance()
