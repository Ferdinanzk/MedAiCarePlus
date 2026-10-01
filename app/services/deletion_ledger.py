"""Durable account deletions, including gallery cleanup after a restore."""

import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime, timezone

from app import config

logger = logging.getLogger(__name__)


async def record(conn, kind: str, u_id: int, object_id: str | None) -> None:
    await conn.execute(
        "INSERT INTO deletion_ledger (kind, u_id, object_id) VALUES ($1,$2,$3)",
        kind, u_id, object_id)


def append_host_file(kind: str, u_id: int, object_id: str | None) -> None:
    try:
        path = config.DELETION_LEDGER_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"kind": kind, "u_id": u_id, "object_id": object_id,
                 "deleted_at": datetime.now(timezone.utc).isoformat()}
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(entry, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        logger.exception("Could not append host deletion ledger for account %s", u_id)


def gallery_files(face_label: str | None) -> list:
    if not face_label or not config.FACE_GALLERY_DIR.exists():
        return []
    # Compare literal filenames, so labels cannot introduce glob or path syntax.
    prefix = f"{face_label}-"
    return sorted(path for path in config.FACE_GALLERY_DIR.iterdir()
                  if path.name.startswith(prefix) and path.suffix == ".jpg"
                  and path.stem[len(prefix):].isdigit() and not path.is_symlink()
                  and path.is_file())


def delete_gallery_files(face_label: str | None) -> None:
    failed = False
    for path in gallery_files(face_label):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed = True
            logger.exception("Could not remove gallery file %s", path.name)
    if failed:
        raise OSError("Gallery cleanup incomplete")


def refresh_gallery(face_label: str | None) -> None:
    from app.services.face_recognition_service import FaceRecognitionService

    service = FaceRecognitionService.get_instance()
    with service.lock:
        service.reload_gallery()
        # A failed unlink or reload must not leave a deleted identity in memory.
        if FaceRecognitionService._available and face_label:
            database = service.face_id.faces_database
            database.database[:] = [identity for identity in database.database
                                    if identity.label != face_label.lower()]


async def replay(conn, lines: Iterable[dict]) -> int:
    count = 0
    for entry in lines:
        if (not isinstance(entry, dict) or entry.get("kind") != "account" or type(entry.get("u_id")) is not int
                or entry["u_id"] <= 0
                or (entry.get("object_id") is not None and not isinstance(entry["object_id"], str))):
            raise ValueError("Invalid or unsupported deletion ledger entry")
        u_id, face_label = entry["u_id"], entry.get("object_id")
        async with conn.transaction():
            # Restore the audit row if this deletion postdates the backup.
            await conn.execute(
                "INSERT INTO deletion_ledger (kind, u_id, object_id) "
                "SELECT 'account', $1, $2 WHERE NOT EXISTS "
                "(SELECT 1 FROM deletion_ledger WHERE kind='account' AND u_id=$1 "
                "AND object_id IS NOT DISTINCT FROM $2)", u_id, face_label)
            await conn.execute('DELETE FROM "user" WHERE u_id=$1', u_id)
        # Unlike the request path, replay fails closed on filesystem errors.
        delete_gallery_files(face_label)
        count += 1
    return count
