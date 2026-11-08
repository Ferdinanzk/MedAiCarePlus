"""Face vectors (8 Nov 2026): face login's gallery is built from the face_vector table, not the photo folder.

A vector is the face-reidentification-retail-0095 descriptor: 256 float32 numbers, stored as 1024 little-endian
bytes. The gallery is a FacesDatabase (the same object, made without reading any folder), so matching is unchanged:
each label's closest vector, then the closest label (MIN_DIST), with FACE_MATCH_THRESHOLD. Vectors and labels are
never logged.
"""

import asyncio
import logging
import os
import re
from pathlib import Path

import numpy as np

from app import config
from app.database import get_pool
from app.services import face_recognition_service as frs

log = logging.getLogger(__name__)

VECTOR_BYTES = 1024   # 256 float32 values

# The label is the owning account's, never the row's own face_label: face login maps a matched label to the account
# with that "user".face_label, so a row whose stored label differed would otherwise log in as another account.
ROWS_SQL = ('SELECT u.face_label, f.vector FROM face_vector f JOIN "user" u USING (u_id) '
            'WHERE u.user_active = TRUE ORDER BY vector_id')
LABELS_SQL = "SELECT DISTINCT lower(face_label) AS label FROM face_vector"
PHOTO_EXTENSIONS = ("jpg", "png")   # FacesDatabase.IMAGE_EXTENSIONS


def to_bytes(desc) -> bytes:
    desc = np.asarray(desc)
    if desc.size != VECTOR_BYTES // 4:
        raise ValueError("a face vector has 256 numbers")
    return desc.astype("<f4").tobytes()


def from_bytes(b: bytes) -> np.ndarray:
    if len(b) != VECTOR_BYTES:
        raise ValueError("a face vector is 1024 bytes")
    return np.frombuffer(b, "<f4")


def build_gallery(rows: list[tuple[str, bytes]]):
    """A FacesDatabase whose .database is one Identity(label, [vectors]) per lower-case label, in row order.
    A row that is not a vector is left out and counted in one WARNING (never its label)."""
    frs._load_imports()
    gallery = object.__new__(frs.FacesDatabase)
    gallery.database = []
    by_label = {}
    skipped = 0
    for label, raw in rows:
        try:
            vector = from_bytes(raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        label = label.lower()
        identity = by_label.get(label)
        if identity is None:
            identity = by_label[label] = frs.FacesDatabase.Identity(label, [])
            gallery.database.append(identity)
        identity.descriptors.append(vector)
    if skipped:
        log.warning("face vectors: %d row(s) skipped, not a 1024-byte vector", skipped)
    return gallery


async def fetch_rows(conn) -> list[tuple[str, bytes]]:
    """The vectors of active accounts, oldest first, each with its owning account's face_label."""
    return [(row["face_label"], row["vector"]) for row in await conn.fetch(ROWS_SQL)]


async def reload(pool=None) -> int:
    """Read the vectors and give face login the gallery built from them. Returns the number of labels."""
    async with (pool or get_pool()).acquire() as conn:
        rows = await fetch_rows(conn)
    gallery = build_gallery(rows)
    service = frs.FaceRecognitionService.get_instance()
    await asyncio.to_thread(service.load_gallery, gallery)   # waits for the service lock off the event loop
    return len(gallery)


async def photos_without_vectors(pool=None, folder=None) -> int:
    """How many photos in the face photo folder have a label with no vector rows (not converted yet, or skipped by
    the conversion).
    Labels are read from the file names as FacesDatabase reads them; temporary dot files are not photos."""
    folder = Path(folder or config.FACE_GALLERY_DIR)
    if not folder.is_dir():
        return 0
    labels = [re.split(r"-\d+$", os.path.splitext(name)[0])[0].lower() for name in os.listdir(folder)
              if not name.startswith(".") and name.split(".")[-1] in PHOTO_EXTENSIONS]
    if not labels:
        return 0
    async with (pool or get_pool()).acquire() as conn:
        converted = {row["label"] for row in await conn.fetch(LABELS_SQL)}
    return sum(label not in converted for label in labels)
