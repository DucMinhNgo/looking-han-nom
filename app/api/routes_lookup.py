"""Search the dataset, and report how well it lines up with the folder.

The mismatch endpoints are not diagnostics bolted on the side — they are half
of what this tool is for. A dataset and a picture folder assembled separately
always drift, and the useful thing is to see by how much.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.auth import current_user
from app.core.dataset import FIELDS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _runtime(request: Request):
    return request.app.state.runtime


@router.get("/lookup")
async def lookup(
    request: Request,
    q: str = Query("", max_length=500),
    field: str = Query("all"),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0, le=200),
    missing_only: bool = Query(False),
    user: dict = Depends(current_user),
):
    """Search by Facebook link, post id, caption, ground truth or filename."""
    runtime = _runtime(request)
    # Resolve before searching: `has_image` is part of what a result shows, and
    # `missing_only` filters on it.
    runtime.refresh()

    page = runtime.dataset.search(
        q,
        field=field,
        offset=offset,
        limit=limit or runtime.settings.page_size,
    )
    payload = page.to_json()

    if missing_only:
        # Filtering after the page would return a short page; redo the search
        # unpaged and cut the window from what actually matched.
        everything = runtime.dataset.search(
            q, field=field, offset=0, limit=len(runtime.dataset.items) or 1
        )
        rows = [i for i in everything.items if not i.has_image]
        limit = limit or runtime.settings.page_size
        window = rows[offset : offset + limit]
        payload.update(
            items=[i.to_json() for i in window],
            total=len(rows),
            has_more=offset + len(window) < len(rows),
        )

    payload["fields"] = list(FIELDS)
    payload["group_tags"] = runtime.settings.group_tags
    return payload


@router.get("/lookup/stats")
async def stats(request: Request, user: dict = Depends(current_user)):
    """Row counts, file counts, and the three mismatch buckets."""
    return _runtime(request).status()


@router.get("/lookup/orphans")
async def orphans(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=500),
    user: dict = Depends(current_user),
):
    """Image files no dataset row claims."""
    report = _runtime(request).report
    names = sorted(report.orphan_images)
    window = names[offset : offset + limit]
    return {
        "items": window,
        "total": len(names),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(window) < len(names),
    }


@router.post("/lookup/rescan")
async def rescan(request: Request, user: dict = Depends(current_user)):
    """Re-read the dataset and re-scan the folder from scratch.

    Both are cached on a change signature, so this is only needed when a file
    was replaced without its mtime moving — but it costs nothing and saves a
    restart when someone is unsure.
    """
    runtime = _runtime(request)
    report = runtime.refresh(force=True)
    return {
        "dataset": runtime.dataset.stats(),
        "images": report.to_json(),
    }


@router.get("/lookup/{index}")
async def one(request: Request, index: int, user: dict = Depends(current_user)):
    runtime = _runtime(request)
    runtime.refresh()
    item = runtime.dataset.get(index)
    if item is None:
        raise HTTPException(404, "no such row")
    return item.to_json()
