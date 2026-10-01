"""Minute job: caregiver confirmation reminders and expiry."""

from app.services import dose_confirmation


async def run_dose_confirmation_maintenance() -> None:
    try:
        await dose_confirmation.maintenance()
    except Exception as exc:
        print(f"[DoseConfirmation] maintenance failed: {exc}")
