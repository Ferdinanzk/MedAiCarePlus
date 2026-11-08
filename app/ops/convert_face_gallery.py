"""Convert the old face photos into face_vector rows, once (face vectors, 8 Nov 2026).

Run it once, with the user's OK, right after the image with face vectors is built and before face login is tested:
    docker compose -p medcareai2 run --rm --no-deps app python -m app.ops.convert_face_gallery
then restart the app, which loads the rows. Until then face login, the robot and the intake monitor do not recognise
the unconverted accounts, so the rebuild, this run and the restart go back to back, outside dose times. The vectors
are computed exactly as the old gallery computed them (FacesDatabase on the folder: the whole stored crop as the
face, no detector), so existing users match as before.
The photo folder is only read, never changed. Only counts are printed; labels and vectors never.
"""

import argparse
import asyncio
import copy
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

from app import config
from app.services import face_recognition_service as frs
from app.services import face_vectors

# Two runs at once: the second waits for the first's commit, then finds its rows (SHARE ROW EXCLUSIVE conflicts with
# itself and with the ROW EXCLUSIVE lock a registration's INSERT takes).
LOCK_SQL = "LOCK TABLE face_vector IN SHARE ROW EXCLUSIVE MODE"
ALREADY_SQL = "SELECT EXISTS (SELECT 1 FROM face_vector WHERE source = 'photo')"
# The latest deletion of an account with that label. face_label is unique, so an account that reuses a label was
# always created after the earlier owner's deletion.
DELETED_SQL = ("SELECT max(deleted_at) FROM deletion_ledger WHERE kind = 'account' "
               "AND lower(object_id) = $1")
OWNER_SQL = ('SELECT u.u_id, u.created_at, (SELECT count(*) FROM face_vector f WHERE f.u_id = u.u_id) AS vectors '
             'FROM "user" u WHERE u.face_label = $1 AND u.user_active = TRUE')
INSERT_SQL = "INSERT INTO face_vector (u_id, face_label, vector, source) VALUES ($1, $2, $3, 'photo')"
TOLERANCE = 1e-6   # cosine distance between a photo's vector and the one read back


class ConversionMismatch(RuntimeError):
    """The rows read back differ from the photos' vectors; the transaction was rolled back."""


def _photo_times(gallery_dir: Path) -> dict[str, list[datetime]]:
    """Each folder label's photo modification times (UTC), with the files and labels FacesDatabase reads: every
    name ending in jpg or png, label = the name without its extension and -<digits>, lower case. Only stat()."""
    times: dict[str, list[datetime]] = {}
    if not os.path.isdir(gallery_dir):
        return times
    for name in os.listdir(gallery_dir):
        if name.split(".")[-1] not in frs.FacesDatabase.IMAGE_EXTENSIONS:
            continue
        label = re.split(r"-\d+$", os.path.splitext(name)[0])[0].lower()
        mtime = os.stat(os.path.join(gallery_dir, name)).st_mtime
        times.setdefault(label, []).append(datetime.fromtimestamp(mtime, timezone.utc))
    return times


def _matches_another(identity, gallery) -> bool:
    """Whether any of identity's vectors is a face login would take for another label of gallery: login's rule
    (FacesDatabase.match_faces with MIN_DIST: each label's closest vector, then the closest label) and its threshold
    (a distance above FACE_MATCH_THRESHOLD is no match; anything else, NaN included, is one)."""
    others = [other for other in gallery.database if other.label != identity.label]
    if not others:
        return False
    view = copy.copy(gallery)
    view.database = others
    for desc in identity.descriptors:
        _, distance = view.match_faces([desc], "MIN_DIST")[0]
        if not config.FACE_MATCH_THRESHOLD < float(distance):
            return True
    return False


def _differs(folder_vectors, stored) -> bool:
    from scipy.spatial.distance import cosine
    if stored is None or len(stored.descriptors) != len(folder_vectors):
        return True
    # "not <=" also catches a NaN distance.
    return any(not cosine(a, b) <= TOLERANCE for a, b in zip(folder_vectors, stored.descriptors))


async def convert(conn, service, gallery_dir: Path) -> dict:
    """Write one 'photo' row per photo of every folder label that belongs to an active account.

    Nothing is written when any 'photo' row exists (already converted), checked under a table lock in the
    transaction, so two runs at once never both write. A label is skipped and counted when:
    - no active account has it (an old backup can bring back a deleted user's photos);
    - an account with that label was deleted (deletion_ledger kind 'account') after the label's oldest photo was
      taken: those photos belonged to the deleted account (a failed file delete, or a restored old backup), and a
      newer account may reuse the label. A deletion older than all the photos does not skip;
    - any of its photos is older than the account (or the account has no created_at): the photos predate it;
    - its account has registered again (its own vectors stay, never mixed with old photos).
    A label that passes those rules is still skipped, and counted apart as matched_other, when any of its photos is
    a face login would take for another label (_matches_another) of the gallery: the stored rows as the transaction
    reads them, plus every label this run converted before it. Otherwise one face would be under two accounts (the
    3 Oct 2026 incident): someone may have registered an unconverted account's face between the rebuild and the
    conversion. Of two folder labels with the same face, the first converted (the folder's reading order) is kept.
    The rows are read back in the same transaction and compared with the folder's vectors; any difference rolls
    everything back and raises ConversionMismatch.
    """
    frs._load_imports()
    folder = frs.FacesDatabase(str(gallery_dir), service.face_id, service.lm_det)   # today's gallery computation
    photo_times = _photo_times(gallery_dir)
    converted = []
    skipped = matched_other = 0
    async with conn.transaction():
        await conn.execute(LOCK_SQL)
        if await conn.fetchval(ALREADY_SQL):
            return {"converted": 0, "labels": 0, "skipped_labels": 0, "matched_other": 0, "already": True}
        gallery = face_vectors.build_gallery(await face_vectors.fetch_rows(conn))   # the accounts' vectors now
        for identity in folder.database:
            owner = await conn.fetchrow(OWNER_SQL, identity.label)
            times = photo_times.get(identity.label)
            if owner is None or owner["vectors"] or owner["created_at"] is None or not times:
                skipped += 1
                continue
            deleted_at = await conn.fetchval(DELETED_SQL, identity.label)
            if (deleted_at is not None and deleted_at > min(times)) or any(t < owner["created_at"] for t in times):
                skipped += 1
                continue
            if _matches_another(identity, gallery):
                matched_other += 1
                continue
            for desc in identity.descriptors:
                await conn.execute(INSERT_SQL, owner["u_id"], identity.label, face_vectors.to_bytes(desc))
            converted.append(identity)
            gallery.database.append(frs.FacesDatabase.Identity(identity.label, list(identity.descriptors)))
        stored = {i.label: i for i in face_vectors.build_gallery(await face_vectors.fetch_rows(conn)).database}
        wrong = sum(_differs(i.descriptors, stored.get(i.label)) for i in converted)
        if wrong:
            raise ConversionMismatch(f"the stored vectors differ from the photos for {wrong} label(s): "
                                     "nothing was written")
    return {"converted": sum(len(i.descriptors) for i in converted), "labels": len(converted),
            "skipped_labels": skipped, "matched_other": matched_other, "already": False}


async def run(gallery_dir: Path) -> dict:
    service = frs.FaceRecognitionService.get_instance()
    if not frs.FaceRecognitionService._available:
        print("The face models are not available: nothing converted.")
        raise SystemExit(1)
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        return await convert(conn, service, gallery_dir)
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", type=Path, default=config.FACE_GALLERY_DIR, help="the face photo folder")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.folder))
    except ConversionMismatch as error:
        print(f"Conversion failed, {error}; the photo folder was not changed.")
        raise SystemExit(1)
    except Exception as error:   # asyncpg.PostgresError and anything else: its text may hold labels, so never print it
        print(f"Conversion failed ({type(error).__name__}); the photo folder was not changed.")
        raise SystemExit(1)
    if result["already"]:
        print("Already converted: nothing written; the photo folder was not changed.")
    else:
        print(f"Converted {result['converted']} photo vector(s) for {result['labels']} account(s); "
              f"{result['skipped_labels']} photo label(s) skipped; the photo folder was not changed.")
        if result["matched_other"]:
            print(f"{result['matched_other']} label(s) skipped: the face matches another account.")


if __name__ == "__main__":
    main()
