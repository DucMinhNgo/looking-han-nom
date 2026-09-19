"""Merging new rows into the dataset, without losing the old ones.

This is the one place in the app that can destroy the user's data, so the rules
are conservative and explicit:

* **Merge, never replace.** A batch adds to what is there. A row whose picture
  is already described replaces that description; everything else is left alone.
* **Identity is the picture's filename**, compared the same tolerant way the
  image library resolves it — lowercased basename. Two batches naming the same
  picture are the same row, however the path was spelled.
* **A row with no picture cannot be identified**, so it is appended rather than
  matched. Deduplicating those on caption text would silently merge two
  genuinely different entries.
* **Order is stable.** Existing rows keep their position so a merge does not
  reshuffle what someone was looking at; new rows land at the end.
* **Every write is atomic and backed up.** The dataset is written to a temp
  file and renamed over the original, and the previous version is kept, because
  a bad upload must never cost a corpus that took months to build.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.core.localimages import basename

log = logging.getLogger(__name__)

# Keep this many previous versions of the dataset.
BACKUP_KEEP = 10


def row_key(row: dict[str, Any]) -> str:
    """The identity of a row: its picture, spelled comparably.

    Empty when the row names no picture — such a row is always treated as new,
    because there is nothing reliable to match it against.
    """
    from app.core.dataset import _pick

    image = _pick(row, "image")
    return basename(image).lower() if image else ""


@dataclass
class MergeResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    malformed: int = 0
    appended_without_image: int = 0

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated)

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "malformed": self.malformed,
            "appended_without_image": self.appended_without_image,
            "changed": self.changed,
        }


def parse_jsonl(data: bytes | str) -> tuple[list[dict[str, Any]], int]:
    """Rows and a count of lines that could not be read.

    One broken line never costs the rest of the upload — it is counted and
    reported so the user can decide whether the batch is good enough.
    """
    if isinstance(data, bytes):
        # utf-8-sig: a file exported from Excel or PowerShell often carries a
        # BOM, which would otherwise land inside the first key's name.
        text = data.decode("utf-8-sig", errors="replace")
    else:
        text = data

    rows: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
        else:
            malformed += 1
    return rows, malformed


def merge(
    existing: Iterable[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
    *,
    malformed: int = 0,
) -> MergeResult:
    """Fold ``incoming`` into ``existing``. Pure — writes nothing."""
    result = MergeResult(malformed=malformed)
    rows = list(existing)

    # Where each identifiable row currently sits, so an update lands in place
    # rather than at the end.
    positions: dict[str, int] = {}
    for index, row in enumerate(rows):
        key = row_key(row)
        if key:
            positions.setdefault(key, index)

    for row in incoming:
        key = row_key(row)
        if not key:
            rows.append(row)
            result.added += 1
            result.appended_without_image += 1
            continue

        index = positions.get(key)
        if index is None:
            positions[key] = len(rows)
            rows.append(row)
            result.added += 1
        elif rows[index] == row:
            result.unchanged += 1
        else:
            rows[index] = row
            result.updated += 1

    result.rows = rows
    return result


def backup(path: Path, backup_dir: Path, keep: int = BACKUP_KEEP) -> Path | None:
    """Copy the current dataset aside before it is overwritten."""
    path, backup_dir = Path(path), Path(backup_dir)
    if not path.is_file():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    # Two merges inside the same second would otherwise land on the same name,
    # and the second would quietly destroy the first — so "keep 10 versions"
    # would not actually be keeping 10. The suffix sorts after the bare stamp,
    # which keeps the pruning below in chronological order.
    attempt = 0
    while target.exists():
        attempt += 1
        target = backup_dir / f"{path.stem}.{stamp}-{attempt}{path.suffix}"
    shutil.copy2(path, target)

    # Keep the most recent few. Unbounded backups of a 40 MB file fill a disk
    # that the pictures also need.
    versions = sorted(backup_dir.glob(f"{path.stem}.*{path.suffix}"))
    for stale in versions[:-keep]:
        stale.unlink(missing_ok=True)

    log.info("backed up %s -> %s", path.name, target.name)
    return target


def write_dataset(path: Path, rows: list[dict[str, Any]]) -> None:
    """Replace the dataset atomically.

    Temp-then-rename, so a reader never sees a half-written file and a crash
    mid-write leaves the previous version intact. ``ensure_ascii=False`` keeps
    the Hán-Nôm readable in the file itself rather than as \\uXXXX escapes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
