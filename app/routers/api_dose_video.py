"""Dose videos for family (services/dose_video.py): the links LINE apps download clips from, and the patient's status.

/api/media/line/* is public on purpose: a family member's LINE app fetches it with no login. The 43-character token
in the path is the only key, one per recipient, and works only while the clip exists. It is the only part of this
router the LINE tunnel proxy forwards (scripts/line-webhook/nginx.conf). FastAPI's FileResponse has no Range
support in this Starlette version, and LINE's player needs it, so ranges are served here.
"""

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.dependencies import get_consented_user
from app.services import dose_video

media_router = APIRouter(prefix="/api/media", tags=["dose-video"])
router = APIRouter(prefix="/api/dose-videos", tags=["dose-video"])

_NAME = re.compile(r"([A-Za-z0-9_-]{%d})\.(mp4|jpg)" % dose_video.TOKEN_LENGTH)
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_TYPES = {"mp4": "video/mp4", "jpg": "image/jpeg"}
_HEADERS = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The (first, last) bytes a Range header asks for; (0, size - 1) for no header, several ranges or a header
    that can't be read (RFC 9110: serve the whole file); None when it can't be satisfied (416)."""
    match = _RANGE.fullmatch((header or "").strip())
    if match is None or not any(match.groups()):
        return 0, size - 1
    first, last = match.groups()
    if not first:   # the last N bytes
        length = int(last)
        return (max(0, size - length), size - 1) if length > 0 and size > 0 else None
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    return (start, end) if start < size and start <= end else None


def _read(path) -> bytes:
    return path.read_bytes()


@media_router.api_route("/line/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def line_media(name: str, request: Request):
    match = _NAME.fullmatch(name)
    opened = await dose_video.open_link(*match.groups()) if match else None
    if opened is None:
        raise HTTPException(404, "Not Found")
    path, link_id = opened
    kind = match.group(2)
    try:
        data = await asyncio.get_running_loop().run_in_executor(None, _read, path)
    except OSError as exc:   # deleted between the lookup and the read
        raise HTTPException(404, "Not Found") from exc
    size = len(data)
    wanted = parse_range(request.headers.get("range"), size)
    if wanted is None:
        return Response(status_code=416, headers={**_HEADERS, "Content-Range": f"bytes */{size}"})
    first, last = wanted
    partial = (first, last) != (0, size - 1)
    headers = {**_HEADERS, "Content-Length": str(last - first + 1)}
    if partial:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    if request.method == "HEAD":
        return Response(status_code=206 if partial else 200, media_type=_TYPES[kind], headers=headers)
    if kind == "mp4" and last == size - 1:
        await dose_video.mark_fetched(link_id)   # this recipient's app now has the end of the clip
    return Response(data[first:last + 1], status_code=206 if partial else 200, media_type=_TYPES[kind],
                    headers=headers)


@router.get("/status")
async def video_status(user: dict = Depends(get_consented_user)):
    return await dose_video.status(user["u_id"])
