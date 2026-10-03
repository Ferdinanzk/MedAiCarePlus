"""Minute job: caregiver confirmation reminders and expiry; also ends abandoned monitor sessions."""

from app.services import dose_confirmation
from app.services.monitor_service import registry


async def run_dose_confirmation_maintenance() -> None:
    try:
        await dose_confirmation.maintenance()
    except Exception as exc:
        print(f"[DoseConfirmation] maintenance failed: {exc}")
    # Interim home (until scheduler.py can take its own entry): a monitor session with no frame for 10 minutes (a
    # closed tab that never sent /end) is ended, so its detector state is freed and its dose gets its emotion result.
    try:
        await registry.sweep_idle()
    except Exception as exc:
        print(f"[DoseConfirmation] idle monitor sweep failed: {exc}")
