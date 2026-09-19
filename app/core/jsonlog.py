"""Small atomic JSON helpers.

Only the account file uses these now, but it is the one file where a torn
write would lock everybody out — so writes go to a temp name, are fsynced, and
are renamed into place. An interrupted write leaves the previous file intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    """The stored value, or ``default`` when the file is absent or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default
