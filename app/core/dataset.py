"""The ground-truth dataset: loading it, and searching it.

One JSONL file, one row per image::

    {"image": "...", "post_id": "https://facebook.com/...",
     "caption": "...", "ground_truth": "..."}

Search is plain case-folded substring, which is the right tool here and not a
shortcut. The content is Hán-Nôm: there is no whitespace to tokenize on, so
word-based indexing would match nothing a reader expects, while a substring of
characters is exactly how someone looks for a line they half remember.

Everything is held in memory and re-read when the file changes. At this size
that is instant, and it means editing the dataset never needs a restart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.models import Item, normalize

log = logging.getLogger(__name__)

FIELDS = ("all", "post", "caption", "ground_truth", "image")

# Tolerated spellings for each field, because the file is produced elsewhere
# and its headers have drifted before.
_ALIASES = {
    "image": ("image", "img", "image_name", "filename", "file"),
    "post_id": ("post_id", "post_url", "post_link", "url", "link", "postid"),
    "caption": ("caption", "fb_caption", "fb caption", "sub_caption"),
    "ground_truth": ("ground_truth", "groundtruth", "ground truth", "gt", "label"),
}


def _pick(row: dict[str, Any], field: str) -> str:
    for alias in _ALIASES[field]:
        value = row.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _pick_url(row: dict[str, Any]) -> str:
    """The first post column that is actually a URL.

    Not the same question as "which column is the post id". An export can
    carry an opaque base64 blob in post_id and the permalink in post_link,
    and taking the first non-empty alias would then hand back the blob and
    leave the row with no link at all.
    """
    for alias in _ALIASES["post_id"]:
        value = str(row.get(alias) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _consumed(row: dict[str, Any]) -> set[str]:
    """The exact column names the canonical fields were read from.

    Everything else belongs in ``extra`` and must survive a rewrite —
    including an alias that went unused, such as post_link sitting beside a
    post_id. Excluding every known alias instead would silently delete that
    column the next time the dataset is written.
    """
    used: set[str] = set()
    for field in _ALIASES:
        for alias in _ALIASES[field]:
            if row.get(alias) not in (None, ""):
                used.add(alias)
                break
    return used


@dataclass
class Page:
    items: list[Item]
    total: int
    offset: int
    limit: int
    query: str = ""
    field: str = "all"

    def to_json(self) -> dict[str, Any]:
        return {
            "items": [i.to_json() for i in self.items],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "query": self.query,
            "field": self.field,
            "has_more": self.offset + len(self.items) < self.total,
        }


class Dataset:
    """The JSONL file, cached until it changes on disk."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: list[Item] | None = None
        self._signature: tuple[float, int] | None = None
        self._malformed = 0

    # --- loading ---------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _file_signature(self) -> tuple[float, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def load(self, force: bool = False) -> list[Item]:
        signature = self._file_signature()
        if signature is None:
            self._items, self._signature, self._malformed = [], None, 0
            return []
        if not force and self._items is not None and signature == self._signature:
            return self._items

        items: list[Item] = []
        malformed = 0
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # One torn line should not cost the whole file; it is
                    # counted so the page can admit the loss.
                    malformed += 1
                    continue
                if not isinstance(row, dict):
                    malformed += 1
                    continue

                items.append(
                    Item(
                        index=len(items),
                        image=_pick(row, "image"),
                        post_id=_pick(row, "post_id"),
                        caption=_pick(row, "caption"),
                        ground_truth=_pick(row, "ground_truth"),
                        post_link=_pick_url(row),
                        # Anything the file carries that was not read into one
                        # of the four fields is kept rather than dropped — it
                        # costs nothing and a column is not lost in transit.
                        extra={k: v for k, v in row.items()
                               if k not in _consumed(row)},
                    )
                )

        self._items = items
        self._signature = signature
        self._malformed = malformed
        log.info(
            "dataset: %d rows from %s%s",
            len(items), self.path.name,
            f" ({malformed} malformed lines skipped)" if malformed else "",
        )
        return items

    @property
    def items(self) -> list[Item]:
        return self.load()

    @property
    def malformed_lines(self) -> int:
        self.load()
        return self._malformed

    # --- searching -------------------------------------------------------

    def search(
        self,
        query: str = "",
        *,
        field: str = "all",
        offset: int = 0,
        limit: int = 24,
    ) -> Page:
        field = field if field in FIELDS else "all"
        needle = normalize(query).strip()
        rows = self.items

        if needle:
            rows = [item for item in rows if needle in item.haystack(field)]

        offset = max(0, offset)
        return Page(
            items=rows[offset : offset + max(1, limit)],
            total=len(rows),
            offset=offset,
            limit=limit,
            query=query,
            field=field,
        )

    def get(self, index: int) -> Item | None:
        items = self.items
        return items[index] if 0 <= index < len(items) else None

    def stats(self) -> dict[str, Any]:
        items = self.items
        return {
            "path": str(self.path),
            "exists": self.exists,
            "rows": len(items),
            "malformed_lines": self._malformed,
            "with_caption": sum(1 for i in items if i.caption),
            "with_ground_truth": sum(1 for i in items if i.ground_truth),
            "with_post_url": sum(1 for i in items if i.post_url),
            "posts": len({i.post_id for i in items if i.post_id}),
        }
