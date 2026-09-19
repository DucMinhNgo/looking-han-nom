"""Serving the local image folder.

Behind the session like the rest of the app — nothing external fetches these
any more, so there is no signature to mint and no anonymous route to guard.

What does still need guarding is the path. The image name reaches this route
from a URL, so containment is checked against the resolved folder root in
``ImageLibrary.path_for`` rather than trusted from the string.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.auth import current_user

log = logging.getLogger(__name__)

router = APIRouter()

CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}


@router.get("/img/{name:path}")
async def serve_image(
    request: Request, name: str, user: dict = Depends(current_user)
):
    library = request.app.state.runtime.images

    # Resolve through the library so a dataset value like "images/x.JPG" finds
    # the file the same way the search did, rather than 404ing on a path the
    # page itself just handed out.
    resolved = library.resolve(name) or name
    path = library.path_for(resolved)
    if path is None:
        raise HTTPException(404, "image not found")

    return FileResponse(
        path,
        media_type=CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )
