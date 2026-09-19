"""Matching dataset rows to the image files actually on disk.

The dataset and the picture folder are produced separately, so they drift. A
row can name a picture nobody copied across; a picture can sit in the folder
with no row describing it. Both happen, and both are worth seeing.

So nothing here silently drops anything. Every row is resolved to a file or
explicitly marked as missing one, every unclaimed file is reported as an
orphan, and the three counts are what the page leads with.

Matching is deliberately tolerant, in descending steps: the exact path, then
the longest trailing part of it, then the basename alone, case-insensitively,
then ignoring the extension. Those cover where mismatches genuinely come from —
a path prefix that was stripped, a ``.JPG`` that became ``.jpg``, a ``.jpeg``
that became ``.jpg``.

The trailing-path step is what makes a folder per post work. When pictures are
filed as ``<post id>/1.jpg`` the basenames stop being unique — nine posts each
have a ``1.jpg`` — so a row naming ``/images/10003.../1.jpg`` has to be matched
on ``10003.../1.jpg``, not on ``1.jpg``. For the same reason a basename shared
by two folders is treated as **ambiguous and refused**: showing one post's
picture beside another post's text is a quiet, convincing kind of wrong, and
far worse than reporting a miss.
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
        self._by_path: dict[str, str] = {}
        self._by_name: dict[str, str] = {}
        self._by_lower: dict[str, str] = {}
        self._by_stem: dict[str, str] = {}
        # Basenames that more than one folder claims. Looking one up returns
        # nothing rather than whichever was walked first.
        self._ambiguous: set[str] = set()
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
        by_path: dict[str, str] = {}
        by_name: dict[str, str] = {}
        by_lower: dict[str, str] = {}
        by_stem: dict[str, str] = {}
        ambiguous: set[str] = set()

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
                    # Full paths are unique by construction, so they index
                    # without any ambiguity question.
                    by_path.setdefault(name, name)
                    by_path.setdefault(name.lower(), name)
                    # Basenames are not. Record the clash instead of letting
                    # whichever directory os.walk reached first win.
                    stem = os.path.splitext(filename)[0].lower()
                    for key, index in (
                        (filename, by_name),
                        (filename.lower(), by_lower),
                        (stem, by_stem),
                    ):
                        if index.setdefault(key, name) != name:
                            ambiguous.add(key)

        names.sort()
        self._files = names
        self._by_path = by_path
        self._by_name, self._by_lower, self._by_stem = by_name, by_lower, by_stem
        self._ambiguous = ambiguous
        self._signature = signature
        self._scanned_at = time.monotonic()
        log.info(
            "image folder: %d files in %s%s",
            len(names), self.images_dir,
            f" ({len(ambiguous)} names shared by more than one folder — "
            "those rows must spell the folder too)" if ambiguous else "",
        )
        return names

    @property
    def files(self) -> list[str]:
        return self.scan()

    # --- matching --------------------------------------------------------

    def resolve(self, image: str) -> str | None:
        """The file this dataset value refers to, or None. Steps, in order."""
        raw = str(image or "").strip().replace("\\", "/").lstrip("/")
        if not raw:
            return None
        self.scan()

        # Longest trailing path first, dropping one leading segment at a time:
        # "/images/10003.../1.jpg" tries "images/10003.../1.jpg", then
        # "10003.../1.jpg" — which is the file — before ever considering the
        # bare "1.jpg". That ordering is what keeps a folder-per-post dataset
        # attached to the right pictures.
        parts = raw.split("/")
        for start in range(len(parts)):
            tail = "/".join(parts[start:])
            hit = self._by_path.get(tail) or self._by_path.get(tail.lower())
            if hit:
                return hit

        # Then the basename on its own, but only when exactly one file has it.
        name = parts[-1]
        stem = Path(name).stem.lower()
        for key, index in (
            (name, self._by_name),
            (name.lower(), self._by_lower),
            (stem, self._by_stem),
        ):
            if key in self._ambiguous:
                continue
            hit = index.get(key)
            if hit:
                return hit
        return None

    def is_ambiguous(self, image: str) -> bool:
        """Whether this row missed only because its filename is not unique."""
        name = basename(image)
        if not name:
            return False
        self.scan()
        return (
            name in self._ambiguous
            or name.lower() in self._ambiguous
            or Path(name).stem.lower() in self._ambiguous
        )

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
