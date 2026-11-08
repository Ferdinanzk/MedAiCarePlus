"""Convert the old face photos into face_vector rows, once (face vectors, 8 Nov 2026).

Run it once, with the user's OK, after the image with face vectors is built and before face login is tested:
    docker compose -p medcareai2 run --rm --no-deps app python -m app.ops.convert_face_gallery
then restart the app, which loads the rows. The vectors are computed exactly as the old gallery computed them
(FacesDatabase on the folder: the whole stored crop as the face, no detector), so existing users match as before.
The photo folder is only read, never changed. Only counts are printed; labels and vectors never.
"""

import argparse
import asyncio
from pathlib import Path

import asyncpg

from app import config
from app.services import face_recognition_service as frs
from app.services import face_vectors

ALREADY_SQL = "SELECT EXISTS (SELECT 1 FROM face_vector WHERE source = 'photo')"
OWNER_SQL = ('SELECT u.u_id, (SELECT count(*) FROM face_vector f WHERE f.u_id = u.u_id) AS vectors '
             'FROM "user" u WHERE u.face_label = $1 AND u.user_active = TRUE')
INSERT_SQL = "INSERT INTO face_vector (u_id, face_label, vector, source) VALUES ($1, $2, $3, 'photo')"
TOLERANCE = 1e-6   # cosine distance between a photo's vector and the one read back


class ConversionMismatch(RuntimeError):
    """The rows read back differ from the photos' vectors; the transaction was rolled back."""


def _differs(folder_vectors, stored) -> bool:
    from scipy.spatial.distance import cosine
    if stored is None or len(stored.descriptors) != len(folder_vectors):
        return True
    # "not <=" also catches a NaN distance.
    return any(not cosine(a, b) <= TOLERANCE for a, b in zip(folder_vectors, stored.descriptors))


async def convert(conn, service, gallery_dir: Path) -> dict:
    """Write one 'photo' row per photo of every folder label that belongs to an active account.

    Nothing is written when any 'photo' row exists (already converted). A label with no active account (an old
    backup can bring back a deleted user's photos), or whose account has registered again (its own vectors stay,
    never mixed with old photos), is skipped and counted. The rows are read back in the same transaction and
    compared with the folder's vectors; any difference rolls everything back and raises ConversionMismatch.
    """
    if await conn.fetchval(ALREADY_SQL):
        return {"converted": 0, "labels": 0, "skipped_labels": 0, "already": True}
    frs._load_imports()
    folder = frs.FacesDatabase(str(gallery_dir), service.face_id, service.lm_det)   # today's gallery computation
    converted = []
    skipped = 0
    async with conn.transaction():
        for identity in folder.database:
            owner = await conn.fetchrow(OWNER_SQL, identity.label)
            if owner is None or owner["vectors"]:
                skipped += 1
                continue
            for desc in identity.descriptors:
                await conn.execute(INSERT_SQL, owner["u_id"], identity.label, face_vectors.to_bytes(desc))
            converted.append(identity)
        stored = {i.label: i for i in face_vectors.build_gallery(await face_vectors.fetch_rows(conn)).database}
        wrong = sum(_differs(i.descriptors, stored.get(i.label)) for i in converted)
        if wrong:
            raise ConversionMismatch(f"the stored vectors differ from the photos for {wrong} label(s): "
                                     "nothing was written")
    return {"converted": sum(len(i.descriptors) for i in converted), "labels": len(converted),
            "skipped_labels": skipped, "already": False}


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
    if result["already"]:
        print("Already converted: nothing written; the photo folder was not changed.")
    else:
        print(f"Converted {result['converted']} photo vector(s) for {result['labels']} account(s); "
              f"{result['skipped_labels']} photo label(s) skipped; the photo folder was not changed.")


if __name__ == "__main__":
    main()
