"""Durable deletions (accounts, chats, memory notes, consent withdrawals), replayed after a restore."""

import json
import logging
import os
import uuid
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
        logger.exception("Could not append host deletion ledger (%s) for account %s", kind, u_id)


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


_DELETE_BY_ID = {
    "conversation": "DELETE FROM conversation WHERE conversation_id = $1::uuid AND u_id = $2",
    "memory": "DELETE FROM patient_memory WHERE memory_id = $1::uuid AND u_id = $2",
}


async def _restore_audit_row(conn, kind: str, u_id: int, object_id: str | None) -> None:
    # Restore the audit row if this deletion postdates the backup. $1 is typed here: otherwise Postgres deduces
    # text from "kind=$1" and varchar from the insert target, and refuses to prepare the statement.
    await conn.execute(
        "INSERT INTO deletion_ledger (kind, u_id, object_id) SELECT $1::text, $2, $3 WHERE NOT EXISTS "
        "(SELECT 1 FROM deletion_ledger WHERE kind=$1 AND u_id=$2 AND object_id IS NOT DISTINCT FROM $3)",
        kind, u_id, object_id)


def _valid_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


async def replay(conn, lines: Iterable[dict]) -> int:
    from app.services import legal_service

    count = 0
    for entry in lines:
        if not isinstance(entry, dict) or type(entry.get("u_id")) is not int or entry["u_id"] <= 0:
            raise ValueError("Invalid or unsupported deletion ledger entry")
        kind, u_id, object_id = entry.get("kind"), entry["u_id"], entry.get("object_id")
        if kind == "account":
            if object_id is not None and not isinstance(object_id, str):
                raise ValueError("Invalid or unsupported deletion ledger entry")
            async with conn.transaction():
                await _restore_audit_row(conn, "account", u_id, object_id)
                await conn.execute('DELETE FROM "user" WHERE u_id=$1', u_id)
            # Unlike the request path, replay fails closed on filesystem errors.
            delete_gallery_files(object_id)
        elif kind in _DELETE_BY_ID:
            if not _valid_uuid(object_id):
                raise ValueError("Invalid or unsupported deletion ledger entry")
            async with conn.transaction():
                await _restore_audit_row(conn, kind, u_id, object_id)
                await conn.execute(_DELETE_BY_ID[kind], object_id, u_id)
        elif kind == "consent":
            if object_id not in legal_service.SCOPE_KIND:
                raise ValueError("Invalid or unsupported deletion ledger entry")
            withdrawn_at = datetime.fromisoformat(str(entry.get("deleted_at")))
            async with conn.transaction():
                await _restore_audit_row(conn, "consent", u_id, object_id)
                # Re-apply the withdrawal only if the restored state still grants it from before then.
                await conn.execute(
                    "INSERT INTO consent (u_id, kind, terms_version, language, document_sha256, scope, granted, source) "
                    "SELECT u_id, kind, terms_version, language, document_sha256, scope, FALSE, 'settings' "
                    "FROM consent WHERE consent_id = (SELECT max(consent_id) FROM consent WHERE u_id = $1 AND scope = $2) "
                    "AND granted AND created_at < $3", u_id, object_id, withdrawn_at)
        else:
            raise ValueError("Invalid or unsupported deletion ledger entry")
        count += 1
    return count
