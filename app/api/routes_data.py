"""Adding data over the web. Admin only.

Everything here writes to files the rest of the app only reads, so three rules
apply to all of it:

* **Admin only.** Search is for everybody; changing the corpus is not.
* **Serialised.** One lock around every write, so two uploads landing together
  cannot interleave into a half-merged dataset.
* **Preview before commit.** A bulk merge reports what it would do and writes
  nothing until asked again. Finding out you have replaced 500 rows *after* the
  fact is not a recoverable kind of surprise — although the backup makes it
  one anyway.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.auth import require_admin
from app.core import ingest, intake
from app.core.intake import IntakeError
from app.core.localimages import basename

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data")

# The dataset is one file rewritten wholesale, so concurrent writers would lose
# each other's rows. A process-wide lock is enough: the app runs one worker.
_write_lock = threading.Lock()

MAX_DATASET_BYTES = 256 * 1024 * 1024


def _runtime(request: Request):
    return request.app.state.runtime


def _rows_of(runtime) -> list[dict]:
    """The dataset as plain rows, ready to be written back.

    ``extra`` comes first so the canonical four always win on a key clash,
    and so a column the app does not model — a gemini_ocr, a post_link —
    still makes the round trip instead of being dropped on the next write.
    """
    return [
        item.extra | {
            "image": item.image, "post_id": item.post_id,
            "caption": item.caption, "ground_truth": item.ground_truth,
        }
        for item in runtime.dataset.items
    ]


async def _read_upload(file: UploadFile, limit: int, what: str) -> bytes:
    data = await file.read()
    if len(data) > limit:
        raise HTTPException(
            413, f"{what} is {len(data) // 1048576} MB, over the "
                 f"{limit // 1048576} MB limit."
        )
    if not data:
        raise HTTPException(400, f"{what} is empty.")
    return data


@router.post("/dataset/preview")
async def preview_dataset(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
):
    """Say what merging this file would do. Writes nothing."""
    data = await _read_upload(file, MAX_DATASET_BYTES, "That file")
    await file.close()

    incoming, malformed = ingest.parse_jsonl(data)
    if not incoming:
        raise HTTPException(
            400,
            "No readable rows in that file. Each line must be one JSON object, "
            f"and {malformed} line(s) could not be parsed."
            if malformed else "No rows in that file.",
        )

    runtime = _runtime(request)
    existing = _rows_of(runtime)

    result = ingest.merge(existing, incoming, malformed=malformed)
    payload = result.to_json()
    payload["filename"] = file.filename or ""
    payload["current_rows"] = len(existing)
    return payload


@router.post("/dataset/merge")
async def merge_dataset(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
):
    """Merge the uploaded rows into the dataset and write it."""
    data = await _read_upload(file, MAX_DATASET_BYTES, "That file")
    await file.close()

    incoming, malformed = ingest.parse_jsonl(data)
    if not incoming:
        raise HTTPException(400, "No readable rows in that file.")

    runtime = _runtime(request)
    path = runtime.settings.dataset_path

    with _write_lock:
        # Re-read inside the lock: another upload may have landed between the
        # preview and this call, and merging onto a stale copy would drop its
        # rows.
        runtime.dataset.load(force=True)
        existing = _rows_of(runtime)

        result = ingest.merge(existing, incoming, malformed=malformed)
        saved = ingest.backup(path, runtime.settings.backups_dir)
        try:
            ingest.write_dataset(path, result.rows)
        except OSError as exc:
            raise HTTPException(
                500,
                f"Could not write {path}: {exc}. If this is Docker, the data "
                "mount is probably still read-only (:ro).",
            ) from exc
        runtime.refresh(force=True)

    log.info("merged %d rows by %r (%d added, %d updated)",
             len(incoming), user["username"], result.added, result.updated)

    payload = result.to_json()
    payload["backup"] = saved.name if saved else None
    payload["images"] = runtime.report.to_json()
    return payload


@router.post("/images")
async def upload_images(
    request: Request,
    files: list[UploadFile] = File(...),
    overwrite: bool = Form(False),
    user: dict = Depends(require_admin),
):
    """Take in pictures — individual files, or a zip of them."""
    runtime = _runtime(request)
    images_dir = runtime.settings.images_dir

    plain: list[tuple[str, bytes]] = []
    result = intake.IntakeResult()
    try:
        with _write_lock:
            for upload in files:
                name = upload.filename or ""
                data = await _read_upload(
                    upload, intake.MAX_ZIP_BYTES, name or "That file"
                )
                await upload.close()

                if intake.is_zip(name):
                    part = intake.extract_zip(
                        data, images_dir, overwrite=overwrite
                    )
                    result.saved += part.saved
                    result.skipped_not_image += part.skipped_not_image
                    result.skipped_conflict += part.skipped_conflict
                    result.skipped_unsafe += part.skipped_unsafe
                    result.bytes_written += part.bytes_written
                else:
                    plain.append((name, data))

            if plain:
                part = intake.save_images(plain, images_dir, overwrite=overwrite)
                result.saved += part.saved
                result.skipped_not_image += part.skipped_not_image
                result.skipped_conflict += part.skipped_conflict
                result.skipped_unsafe += part.skipped_unsafe
                result.bytes_written += part.bytes_written

            runtime.refresh(force=True)
    except IntakeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            500,
            f"Could not write to {images_dir}: {exc}. If this is Docker, the "
            "image mount is probably still read-only (:ro).",
        ) from exc

    log.info("%d pictures taken in by %r", len(result.saved), user["username"])
    payload = result.to_json()
    payload["images"] = runtime.report.to_json()
    return payload


@router.post("/row")
async def add_row(
    request: Request,
    post_id: str = Form(""),
    caption: str = Form(""),
    ground_truth: str = Form(""),
    image_name: str = Form(""),
    file: UploadFile | None = File(None),
    user: dict = Depends(require_admin),
):
    """Add or update one row, optionally with its picture, from a form."""
    runtime = _runtime(request)

    name = (image_name or "").strip()
    if file is not None and file.filename:
        data = await _read_upload(file, intake.MAX_IMAGE_BYTES, "That picture")
        await file.close()
        try:
            with _write_lock:
                saved = intake.save_images(
                    [(file.filename, data)],
                    runtime.settings.images_dir,
                    overwrite=True,
                )
        except IntakeError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not saved.saved:
            raise HTTPException(
                400, f"{file.filename} is not an image file this app can show."
            )
        name = saved.saved[0]

    if not name:
        raise HTTPException(400, "Give the row a picture, or name an existing one.")
    if not (ground_truth.strip() or caption.strip()):
        raise HTTPException(400, "A row needs a caption or a ground truth.")

    row = {
        "image": name,
        "post_id": post_id.strip(),
        "caption": caption.strip(),
        "ground_truth": ground_truth.strip(),
    }

    path = runtime.settings.dataset_path
    with _write_lock:
        runtime.dataset.load(force=True)
        existing = _rows_of(runtime)

        result = ingest.merge(existing, [row])
        ingest.backup(path, runtime.settings.backups_dir)
        try:
            ingest.write_dataset(path, result.rows)
        except OSError as exc:
            raise HTTPException(500, f"Could not write {path}: {exc}") from exc
        runtime.refresh(force=True)

    return {
        "row": row,
        "added": result.added,
        "updated": result.updated,
        "total": result.total,
        "images": runtime.report.to_json(),
    }


@router.post("/ground-truth")
async def edit_ground_truth(
    request: Request,
    index: int = Form(...),
    ground_truth: str = Form(...),
    expect_image: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Correct one row's ground truth in place.

    Addressed by row number, but checked against the picture the caller
    believed was there. A merge can move rows while an editor has the page
    open, and writing a correction over the wrong row is exactly the kind of
    quiet damage a corpus never recovers from.
    """
    runtime = _runtime(request)
    path = runtime.settings.dataset_path

    with _write_lock:
        runtime.dataset.load(force=True)
        rows = _rows_of(runtime)
        if not 0 <= index < len(rows):
            raise HTTPException(
                404, f"Row {index + 1} is no longer there — the dataset now "
                     f"has {len(rows)} rows. Reload the page."
            )

        here = str(rows[index].get("image") or "")
        if expect_image and basename(here).lower() != basename(expect_image).lower():
            raise HTTPException(
                409,
                f"Row {index + 1} is now {here or '(no picture)'}, not "
                f"{expect_image}. Someone changed the dataset while this page "
                "was open. Reload and try again.",
            )

        if str(rows[index].get("ground_truth") or "") == ground_truth:
            return {"changed": False, "index": index,
                    "ground_truth": ground_truth, "backup": None}

        rows[index]["ground_truth"] = ground_truth
        saved = ingest.backup(path, runtime.settings.backups_dir)
        try:
            ingest.write_dataset(path, rows)
        except OSError as exc:
            raise HTTPException(
                500,
                f"Could not write {path}: {exc}. If this is Docker, the data "
                "mount is probably still read-only (:ro).",
            ) from exc
        runtime.refresh(force=True)

    log.info("row %d ground truth edited by %r", index + 1, user["username"])
    return {
        "changed": True,
        "index": index,
        "ground_truth": ground_truth,
        "backup": saved.name if saved else None,
    }


@router.post("/verify")
async def verify_row(
    request: Request,
    index: int = Form(...),
    expect_image: str = Form(""),
    verified: bool = Form(True),
    user: dict = Depends(require_admin),
):
    """Mark a row as verified without changing its text."""
    runtime = _runtime(request)
    path = runtime.settings.dataset_path
    with _write_lock:
        runtime.dataset.load(force=True)
        rows = _rows_of(runtime)
        if not 0 <= index < len(rows):
            raise HTTPException(404, "Row is no longer in the dataset. Reload the page.")
        here = str(rows[index].get("image") or "")
        if expect_image and basename(here).lower() != basename(expect_image).lower():
            raise HTTPException(409, "Dataset changed underneath this page. Reload and try again.")
        rows[index]["verified"] = bool(verified)
        rows[index]["verified_by"] = user["username"] if verified else ""
        rows[index]["verified_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds") if verified else ""
        )
        saved = ingest.backup(path, runtime.settings.backups_dir)
        try:
            ingest.write_dataset(path, rows)
        except OSError as exc:
            raise HTTPException(500, f"Could not write {path}: {exc}") from exc
        runtime.refresh(force=True)
    return {
        "changed": True, "index": index, "verified": bool(verified),
        "verified_by": rows[index]["verified_by"],
        "verified_at": rows[index]["verified_at"],
        "backup": saved.name if saved else None,
    }


@router.get("/backups")
async def list_backups(request: Request, user: dict = Depends(require_admin)):
    """Previous versions of the dataset, newest first."""
    backups_dir = _runtime(request).settings.backups_dir
    if not backups_dir.is_dir():
        return {"items": []}
    items = []
    for path in sorted(backups_dir.glob("*.jsonl"), reverse=True):
        info = path.stat()
        items.append({
            "name": path.name,
            "bytes": info.st_size,
            "mb": round(info.st_size / 1048576, 2),
        })
    return {"items": items}


@router.get("/backups/{name}")
async def download_backup(
    request: Request, name: str, user: dict = Depends(require_admin)
):
    """Hand back one previous version, so a bad merge can be undone by hand."""
    backups_dir = _runtime(request).settings.backups_dir
    try:
        root = backups_dir.resolve()
        target = (backups_dir / name).resolve()
    except OSError:
        raise HTTPException(404, "no such backup") from None
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "no such backup")
    return FileResponse(
        target, media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )
