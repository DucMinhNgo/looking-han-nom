"""Taking pictures in over the web, safely.

Two ways in: individual files, or a zip of them. The zip is what makes a batch
of five hundred practical, and it is also where the danger is — a zip entry can
name ``../../etc/passwd`` and a naive extract will happily write there. So every
entry's destination is resolved and checked against the folder root before a
single byte is written, and entries that are not plain image files are skipped
rather than trusted.

Nothing here overwrites by default. A picture arriving under a name that is
already taken is reported as a conflict and left alone, because silently
replacing one image with another would change what a row means without anybody
noticing.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from app.core.localimages import IMAGE_SUFFIXES, is_image

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024
# A zip that expands to far more than it claims is a decompression bomb; this
# is a plain sanity ceiling on what one upload may unpack to.
MAX_UNPACKED_BYTES = 4 * 1024 * 1024 * 1024


class IntakeError(ValueError):
    """Something the uploader can fix and retry."""


@dataclass
class IntakeResult:
    saved: list[str] = field(default_factory=list)
    skipped_not_image: list[str] = field(default_factory=list)
    skipped_conflict: list[str] = field(default_factory=list)
    skipped_unsafe: list[str] = field(default_factory=list)
    bytes_written: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "saved": len(self.saved),
            "saved_sample": sorted(self.saved)[:50],
            "skipped_not_image": len(self.skipped_not_image),
            "skipped_conflict": len(self.skipped_conflict),
            "conflict_sample": sorted(self.skipped_conflict)[:50],
            "skipped_unsafe": len(self.skipped_unsafe),
            "bytes_written": self.bytes_written,
            "mb_written": round(self.bytes_written / 1048576, 2),
        }


def safe_target(
    images_dir: Path, name: str, *, keep_folders: bool = False
) -> Path | None:
    """Where this name may be written, or None if it may not be.

    The check is on the resolved path against the resolved root, not on the
    string: that is what actually stops ``../`` and an absolute path, and it
    costs nothing to do per entry.

    ``keep_folders`` is for zips. A dataset that files pictures one folder per
    post depends on those folders — flattening it would collapse every post's
    ``1.jpg`` into a single name, so nine of ten would be reported as
    conflicts and the tenth would be attached to whichever row asked first.
    Individual file uploads still flatten: a browser hands over a bare
    filename anyway, and a path in one would only have come from the string.
    """
    cleaned = str(name or "").strip().replace("\\", "/").lstrip("/")
    if not cleaned or cleaned.endswith("/"):
        return None

    parts = [p for p in cleaned.split("/") if p not in ("", ".", "..")]
    if not parts:
        return None
    relative = "/".join(parts) if keep_folders else parts[-1]

    try:
        root = images_dir.resolve()
        target = (images_dir / relative).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root):
        log.warning("rejected unsafe upload name %r", name)
        return None
    return target


def _relative(target: Path, images_dir: Path) -> str:
    """The name the rest of the app knows this file by: relative, forward
    slashes — the same spelling ``ImageLibrary.scan`` indexes."""
    try:
        return target.resolve().relative_to(images_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return target.name


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)


def save_images(
    files: Iterable[tuple[str, bytes]],
    images_dir: Path,
    *,
    overwrite: bool = False,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> IntakeResult:
    """Store uploaded pictures, one (filename, bytes) pair at a time."""
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    result = IntakeResult()

    for name, data in files:
        leaf = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not is_image(leaf):
            result.skipped_not_image.append(leaf or "(no name)")
            continue
        if len(data) > max_bytes:
            raise IntakeError(
                f"{leaf} is {len(data) // 1048576} MB, over the "
                f"{max_bytes // 1048576} MB limit."
            )

        target = safe_target(images_dir, leaf)
        if target is None:
            result.skipped_unsafe.append(leaf)
            continue
        if target.exists() and not overwrite:
            result.skipped_conflict.append(leaf)
            continue

        _write(target, data)
        result.saved.append(target.name)
        result.bytes_written += len(data)

    log.info("intake: %d saved, %d conflicts, %d not images",
             len(result.saved), len(result.skipped_conflict),
             len(result.skipped_not_image))
    return result


def extract_zip(
    stream: BinaryIO | bytes,
    images_dir: Path,
    *,
    overwrite: bool = False,
    max_unpacked: int = MAX_UNPACKED_BYTES,
) -> IntakeResult:
    """Unpack the image files out of a zip, ignoring everything else."""
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    result = IntakeResult()

    data = stream if isinstance(stream, bytes) else stream.read()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IntakeError(f"That file is not a readable zip ({exc}).") from exc

    with archive:
        planned = sum(
            info.file_size for info in archive.infolist() if not info.is_dir()
        )
        if planned > max_unpacked:
            raise IntakeError(
                f"The zip unpacks to {planned // 1048576} MB, over the "
                f"{max_unpacked // 1048576} MB limit."
            )

        for info in archive.infolist():
            if info.is_dir():
                continue
            leaf = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            # Mac archives carry a __MACOSX/ shadow tree of metadata files that
            # look like the real ones; they are not pictures.
            if info.filename.startswith("__MACOSX/") or leaf.startswith("._"):
                result.skipped_not_image.append(leaf)
                continue
            if not is_image(leaf):
                result.skipped_not_image.append(leaf or "(no name)")
                continue

            # Folders are kept: see safe_target. The zip-slip guard is the
            # resolved containment check there, not the flattening.
            target = safe_target(images_dir, info.filename, keep_folders=True)
            if target is None:
                result.skipped_unsafe.append(info.filename)
                continue
            # Report the path, not the leaf, or ten posts' worth of "1.jpg"
            # all read as the same conflict.
            shown = _relative(target, images_dir)
            if target.exists() and not overwrite:
                result.skipped_conflict.append(shown)
                continue

            with archive.open(info) as handle:
                _write(target, handle.read())
            result.saved.append(shown)
            result.bytes_written += info.file_size

    log.info("zip intake: %d saved, %d conflicts, %d skipped",
             len(result.saved), len(result.skipped_conflict),
             len(result.skipped_not_image) + len(result.skipped_unsafe))
    return result


def is_zip(filename: str) -> bool:
    return Path(str(filename or "")).suffix.lower() == ".zip"


ALLOWED_SUFFIXES = IMAGE_SUFFIXES | {".zip"}
