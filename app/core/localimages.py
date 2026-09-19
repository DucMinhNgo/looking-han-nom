"""Matching dataset rows to the image files actually on disk.

The dataset and the picture folder are produced separately, so they drift. A
row can name a picture nobody copied across; a picture can sit in the folder
with no row describing it. Both happen, and both are worth seeing.

So nothing here silently drops anything. Every row is resolved to a file or
explicitly marked as missing one, every unclaimed file is reported as an
orphan, and the three counts are what the page leads with.

Matching is deliberately tolerant, in four descending steps: the exact string,
then the basename only, then case-insensitively, then ignoring the extension.
Those four cover where mismatches genuinely come from — a path prefix that was
stripped, a ``.JPG`` that became ``.jpg``, a ``.jpeg`` that became ``.jpg``.
Anything looser would start matching the wrong picture to the wrong text, which
is far worse than reporting a miss.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
}


def is_image(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in IMAGE_SUFFIXES


def basename(value: str) -> str:
    """Filename only — the dataset may carry ``images/x.jpg`` or ``x.jpg``."""
    return str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]


@dataclass
class MatchReport:
    """What the last scan found. The headline numbers of the whole page."""

    scanned: int = 0
    matched: int = 0
    rows_without_image: int = 0
    orphan_images: list[str] = field(default_factory=list)
    scanned_at: float = 0.0
    directory: str = ""
    exists: bool = True

    @property
    def orphans(self) -> int:
        return len(self.orphan_images)

    def to_json(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "exists": self.exists,
            "files": self.scanned,
            "matched": self.matched,
            "rows_without_image": self.rows_without_image,
            "orphan_images": self.orphans,
            # Capped: a folder can hold thousands of orphans and the page only
            # needs enough of them to make the problem concrete.
            "orphan_sample": sorted(self.orphan_images)[:200],
            "scanned_at": self.scanned_at,
        }


# How long a scan is trusted before being redone. The signature below catches
# anything added to or removed from the top level at once, but NOT a change
# inside a sub-folder — a parent's mtime does not move when a child changes —
# and not a file replaced under its own name. Walking the whole tree on every
# request would mean stat-ing thousands of files, so those two cases are
# bounded by time instead, and the Rescan button is there for anyone who does
# not want to wait.
DEFAULT_TTL_S = 30.0


class ImageLibrary:
    """The picture folder, indexed for tolerant lookup."""

    def __init__(self, images_dir: Path, ttl_s: float = DEFAULT_TTL_S) -> None:
        self.images_dir = Path(images_dir)
        self.ttl_s = ttl_s
        self._by_name: dict[str, str] = {}
        self._by_lower: dict[str, str] = {}
        self._by_stem: dict[str, str] = {}
        self._signature: tuple[float, int, int] | None = None
        self._scanned_at: float = 0.0
        self._files: list[str] = []

    # --- scanning --------------------------------------------------------

    def _directory_signature(self) -> tuple[float, int, int] | None:
        """Cheap change detection, without walking the tree.

        The entry count is in here because the directory mtime alone is not
        trustworthy: Windows timestamps land on a ~15 ms tick, so a file added
        or removed in the same tick as the previous scan leaves the mtime
        unchanged and the change invisible. Counting the top level costs one
        readdir — no per-file stat — and makes add and delete reliable.

        A file *replaced* under the same name moves neither, which is what the
        TTL is for.
        """
        try:
            info = self.images_dir.stat()
            with os.scandir(self.images_dir) as entries:
                count = sum(1 for _ in entries)
        except OSError:
            return None
        return (info.st_mtime, info.st_size, count)

    def scan(self, force: bool = False) -> list[str]:
        """Index the folder, reusing the previous scan when nothing changed.

        Redone when the caller insists, when the directory's own mtime moved,
        or when the last scan has gone stale — see ``DEFAULT_TTL_S`` for why
        the third condition is needed at all.
        """
        import time

        signature = self._directory_signature()
        fresh = time.monotonic() - self._scanned_at < self.ttl_s
        if (
            not force
            and fresh
            and self._signature is not None
            and signature == self._signature
        ):
            return self._files

        names: list[str] = []
        by_name: dict[str, str] = {}
        by_lower: dict[str, str] = {}
        by_stem: dict[str, str] = {}

        if self.images_dir.is_dir():
            root = str(self.images_dir)
            # os.walk over Path.rglob on purpose: it already knows which entries
            # are files, so this costs one readdir per directory instead of a
            # stat per file. On 9,000 images that is the difference between
            # about 30 ms and about a second.
            for dirpath, _dirnames, filenames in os.walk(root):
                prefix = os.path.relpath(dirpath, root).replace(os.sep, "/")
                prefix = "" if prefix == "." else prefix + "/"
                for filename in sorted(filenames):
                    if not is_image(filename):
                        continue
                    name = prefix + filename
                    names.append(name)
                    # First writer wins at every level, so a nested duplicate
                    # never displaces the file whose name matched exactly.
                    by_name.setdefault(name, name)
                    by_name.setdefault(filename, name)
                    by_lower.setdefault(filename.lower(), name)
                    by_stem.setdefault(os.path.splitext(filename)[0].lower(), name)

        names.sort()
        self._files = names
        self._by_name, self._by_lower, self._by_stem = by_name, by_lower, by_stem
        self._signature = signature
        self._scanned_at = time.monotonic()
        log.info("image folder: %d files in %s", len(names), self.images_dir)
        return names

    @property
    def files(self) -> list[str]:
        return self.scan()

    # --- matching --------------------------------------------------------

    def resolve(self, image: str) -> str | None:
        """The file this dataset value refers to, or None. Four steps, in order."""
        raw = str(image or "").strip().replace("\\", "/")
        if not raw:
            return None
        self.scan()

        name = basename(raw)
        for candidate in (
            self._by_name.get(raw),
            self._by_name.get(name),
            self._by_lower.get(name.lower()),
            self._by_stem.get(Path(name).stem.lower()),
        ):
            if candidate:
                return candidate
        return None

    def path_for(self, name: str) -> Path | None:
        """The file on disk for a name this library returned.

        Resolves and re-checks containment rather than trusting the name: this
        is what stops a crafted request reading outside the folder, and it is
        cheap enough to do on every request.
        """
        if not name:
            return None
        try:
            root = self.images_dir.resolve()
            candidate = (self.images_dir / name).resolve()
        except OSError:
            return None
        if not candidate.is_relative_to(root):
            log.warning("rejected path escape for image %r", name)
            return None
        return candidate if candidate.is_file() else None

    # --- the whole picture -----------------------------------------------

    def attach(self, items: Iterable[Any]) -> MatchReport:
        """Resolve every row, and report what did not line up.

        Mutates each item's ``image_name``/``has_image`` in place, because the
        alternative — a parallel dict every caller has to carry — gets out of
        step with the items themselves.
        """
        import time

        files = self.scan()
        claimed: set[str] = set()
        matched = missing = 0

        for item in items:
            resolved = self.resolve(item.image)
            item.image_name = resolved or ""
            item.has_image = resolved is not None
            if resolved:
                claimed.add(resolved)
                matched += 1
            else:
                missing += 1

        return MatchReport(
            scanned=len(files),
            matched=matched,
            rows_without_image=missing,
            orphan_images=[name for name in files if name not in claimed],
            scanned_at=time.time(),
            directory=str(self.images_dir),
            exists=self.images_dir.is_dir(),
        )
