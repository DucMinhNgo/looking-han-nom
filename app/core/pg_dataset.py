"""PostgreSQL-backed dataset — drop-in thay thế cho Dataset (JSONL).

Read : SELECT từ bảng ``dataset_items``, cache in-memory.
Search : Python in-memory filter — giống Dataset cũ, đơn giản và đủ nhanh.
Write : INSERT ON CONFLICT UPDATE cho merge/add-row;
        UPDATE trực tiếp cho ground-truth và verify.

Cache invalidation: so sánh ``MAX(updated_at)`` trước mỗi lần đọc.
Nếu timestamp không đổi, cache được giữ nguyên — không phải query lại DB.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from app.core.dataset import FIELDS, Page
from app.core.ingest import MergeResult, merge
from app.core.models import Item, normalize

log = logging.getLogger(__name__)


# ─── SQL ──────────────────────────────────────────────────────────────────────

_SQL_LOAD = """
SELECT id, image, post_id, post_link, caption, ground_truth, phonetic, extra
    FROM dataset_items
 ORDER BY display_index NULLS LAST, id
"""

_SQL_SIGNATURE = "SELECT MAX(updated_at) FROM dataset_items"

_SQL_UPSERT_IMAGE = """
INSERT INTO dataset_items
    (image, post_id, post_link, caption, ground_truth, phonetic, extra, display_index)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (image) WHERE image <> ''
DO UPDATE SET
    post_id       = EXCLUDED.post_id,
    post_link     = EXCLUDED.post_link,
    caption       = EXCLUDED.caption,
    ground_truth  = EXCLUDED.ground_truth,
    phonetic      = EXCLUDED.phonetic,
    extra         = EXCLUDED.extra,
    display_index = EXCLUDED.display_index,
    updated_at    = NOW()
WHERE
    dataset_items.post_id       IS DISTINCT FROM EXCLUDED.post_id       OR
    dataset_items.post_link     IS DISTINCT FROM EXCLUDED.post_link     OR
    dataset_items.caption       IS DISTINCT FROM EXCLUDED.caption       OR
    dataset_items.ground_truth  IS DISTINCT FROM EXCLUDED.ground_truth  OR
    dataset_items.phonetic      IS DISTINCT FROM EXCLUDED.phonetic      OR
    dataset_items.extra         IS DISTINCT FROM EXCLUDED.extra         OR
    dataset_items.display_index IS DISTINCT FROM EXCLUDED.display_index
RETURNING id
"""

_SQL_UPSERT_NO_IMAGE = """
INSERT INTO dataset_items
    (image, post_id, post_link, caption, ground_truth, phonetic, extra, display_index)
VALUES ('', %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (post_id) WHERE image = '' AND post_id <> ''
DO UPDATE SET
    post_link     = EXCLUDED.post_link,
    caption       = EXCLUDED.caption,
    ground_truth  = EXCLUDED.ground_truth,
    phonetic      = EXCLUDED.phonetic,
    extra         = EXCLUDED.extra,
    display_index = EXCLUDED.display_index,
    updated_at    = NOW()
RETURNING id
"""

_SQL_INSERT_NO_KEY = """
INSERT INTO dataset_items
    (image, post_id, post_link, caption, ground_truth, phonetic, extra, display_index)
VALUES ('', '', %s, %s, %s, %s, %s, %s)
RETURNING id
"""

_SQL_UPDATE_GROUND_TRUTH = """
UPDATE dataset_items
   SET ground_truth = %s, updated_at = NOW()
 WHERE id = %s
"""

_SQL_UPDATE_PHONETIC = """
UPDATE dataset_items
     SET phonetic = %s, updated_at = NOW()
 WHERE id = %s
"""

_SQL_UPDATE_EXTRA = """
UPDATE dataset_items
   SET extra = %s, updated_at = NOW()
 WHERE id = %s
"""


# ─── Connection helper ─────────────────────────────────────────────────────────

def _open_conn(url: str):
    """Thử psycopg3 trước, fallback psycopg2."""
    try:
        import psycopg
        return psycopg.connect(url, autocommit=False), "psycopg3"
    except ImportError:
        pass
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        psycopg2.extras.register_default_jsonb(conn)
        return conn, "psycopg2"
    except ImportError:
        raise RuntimeError(
            "Cần psycopg3 hoặc psycopg2: pip install 'psycopg[binary]'"
        )


# ─── PgDataset ─────────────────────────────────────────────────────────────────

class PgDataset:
    """Dataset items lưu trong PostgreSQL.

    Interface giống ``Dataset``:
      .items, .exists, .malformed_lines
      load(), search(), get(), stats()

    Thêm các write methods mà Dataset không có:
      upsert_rows(), update_ground_truth(), update_verify()
    """

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._items: list[Item] = []
        self._db_ids: list[int] = []        # DB id song song với _items
        self._signature: str | None = None  # MAX(updated_at)
        self._lock = threading.Lock()
        self._conn = None
        self._driver: str = ""

    # ─── Connection management ─────────────────────────────────────────────────

    def _get_conn(self):
        if self._conn is None:
            self._conn, self._driver = _open_conn(self._url)
            return self._conn
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1")
            self._conn.commit()
            return self._conn
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn, self._driver = _open_conn(self._url)
            return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._get_conn()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            conn.commit()
            try:
                return cur.fetchall()
            except Exception:
                return []
        except Exception:
            conn.rollback()
            raise

    def _json_param(self, value: dict) -> Any:
        """Dict → kiểu phù hợp với driver đang dùng."""
        if self._driver == "psycopg3":
            return json.dumps(value, ensure_ascii=False)
        try:
            from psycopg2.extras import Json
            return Json(value)
        except ImportError:
            return json.dumps(value, ensure_ascii=False)

    # ─── Interface (giống Dataset) ─────────────────────────────────────────────

    @property
    def exists(self) -> bool:
        try:
            self._execute("SELECT 1 FROM dataset_items LIMIT 1")
            return True
        except Exception:
            return False

    @property
    def malformed_lines(self) -> int:
        return 0

    def _db_signature(self) -> str | None:
        try:
            rows = self._execute(_SQL_SIGNATURE)
            if rows and rows[0][0]:
                return str(rows[0][0])
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_extra(raw: Any) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                pass
        return {}

    def load(self, force: bool = False) -> list[Item]:
        sig = self._db_signature()
        with self._lock:
            if not force and self._items and sig == self._signature:
                return self._items
            try:
                rows = self._execute(_SQL_LOAD)
            except Exception as exc:
                log.error("PgDataset.load() thất bại: %s", exc)
                return self._items  # Trả về cache cũ thay vì []

            items: list[Item] = []
            db_ids: list[int] = []
            for i, (db_id, image, post_id, post_link, caption, ground_truth, phonetic, extra) in enumerate(rows):
                items.append(Item(
                    index=i,
                    image=image or "",
                    post_id=post_id or "",
                    post_link=post_link or "",
                    caption=caption or "",
                    ground_truth=ground_truth or "",
                    phonetic=phonetic or "",
                    extra=self._parse_extra(extra),
                ))
                db_ids.append(db_id)

            self._items = items
            self._db_ids = db_ids
            self._signature = sig
            log.info("pg dataset: %d rows loaded", len(items))
            return items

    @property
    def items(self) -> list[Item]:
        return self.load()

    def search(
        self,
        query: str = "",
        *,
        field: str = "all",
        offset: int = 0,
        limit: int = 24,
    ) -> Page:
        """In-memory filter — cùng logic với Dataset.search."""
        field = field if field in FIELDS else "all"
        needle = normalize(query).strip()
        rows = self.items
        if needle:
            rows = [item for item in rows if needle in item.haystack(field)]
        offset = max(0, offset)
        return Page(
            items=rows[offset: offset + max(1, limit)],
            total=len(rows),
            offset=offset,
            limit=limit,
            query=query,
            field=field,
        )

    def get(self, index: int) -> Item | None:
        items = self.items
        return items[index] if 0 <= index < len(items) else None

    def stats(self) -> dict:
        items = self.items
        return {
            "path": "postgresql://dataset_items",
            "exists": True,
            "rows": len(items),
            "malformed_lines": 0,
            "with_caption": sum(1 for i in items if i.caption),
            "with_ground_truth": sum(1 for i in items if i.ground_truth),
            "with_phonetic": sum(1 for i in items if i.phonetic),
            "verified": sum(1 for i in items if i.extra.get("verified") is True),
            "with_post_url": sum(1 for i in items if i.post_url),
            "posts": len({i.post_id for i in items if i.post_id}),
        }

    # ─── Write methods (không có trong Dataset) ────────────────────────────────

    def upsert_rows(
        self,
        incoming: list[dict],
        malformed: int = 0,
    ) -> MergeResult:
        """Merge ``incoming`` vào DB.

        Dùng cùng ``ingest.merge()`` để tính added/updated/unchanged,
        rồi bulk-upsert toàn bộ result.rows vào PostgreSQL.
        Cache bị xoá để lần đọc tiếp sẽ reload từ DB.
        """
        existing = self._items_as_rows()
        result = merge(existing, incoming, malformed=malformed)

        if not result.changed:
            return result

        conn = self._get_conn()
        cur = conn.cursor()
        try:
            for display_idx, row in enumerate(result.rows):
                self._upsert_one(cur, row, display_idx)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise RuntimeError(f"Lỗi DB khi upsert: {exc}") from exc

        # Invalidate cache → load() tiếp theo sẽ re-fetch
        with self._lock:
            self._signature = None

        return result

    def _upsert_one(self, cur, row: dict, display_idx: int) -> None:
        """Upsert một row vào DB qua cursor đang mở."""
        from app.core.dataset import _pick, _pick_url, _consumed

        image = _pick(row, "image")
        post_id = _pick(row, "post_id")
        post_link = _pick_url(row)
        caption = _pick(row, "caption")
        ground_truth = _pick(row, "ground_truth")
        phonetic = _pick(row, "phonetic")
        extra = {k: v for k, v in row.items() if k not in _consumed(row)}
        ep = self._json_param(extra)

        if image:
            cur.execute(_SQL_UPSERT_IMAGE,
                        (image, post_id, post_link, caption, ground_truth, phonetic, ep, display_idx))
        elif post_id:
            cur.execute(_SQL_UPSERT_NO_IMAGE,
                        (post_id, post_link, caption, ground_truth, phonetic, ep, display_idx))
        else:
            cur.execute(_SQL_INSERT_NO_KEY,
                        (post_link, caption, ground_truth, phonetic, ep, display_idx))

    def update_ground_truth(
        self, index: int, ground_truth: str
    ) -> None:
        """UPDATE ground_truth tại index; giữ cache in-memory đồng bộ."""
        with self._lock:
            if not 0 <= index < len(self._items):
                raise IndexError(index)
            db_id = self._db_ids[index]

        self._execute(_SQL_UPDATE_GROUND_TRUTH, (ground_truth, db_id))

        with self._lock:
            if index < len(self._items):
                self._items[index].ground_truth = ground_truth

    def update_phonetic(self, index: int, phonetic: str) -> None:
        """UPDATE phonetic tại index; giữ cache in-memory đồng bộ."""
        with self._lock:
            if not 0 <= index < len(self._items):
                raise IndexError(index)
            db_id = self._db_ids[index]

        self._execute(_SQL_UPDATE_PHONETIC, (phonetic, db_id))

        with self._lock:
            if index < len(self._items):
                self._items[index].phonetic = phonetic

    def update_verify(
        self, index: int, verified: bool, username: str
    ) -> dict:
        """UPDATE extra (verified, verified_by, verified_at, history) tại index."""
        from datetime import datetime, timezone

        with self._lock:
            if not 0 <= index < len(self._items):
                raise IndexError(index)
            item = self._items[index]
            db_id = self._db_ids[index]

        verified_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        history = list(item.extra.get("verify_history", []))
        if not isinstance(history, list):
            history = []
        history.append({"verified": bool(verified), "username": username, "at": verified_at})

        new_extra = {
            **item.extra,
            "verified": bool(verified),
            "verified_by": username,
            "verified_at": verified_at,
            "verify_history": history,
        }
        self._execute(_SQL_UPDATE_EXTRA, (self._json_param(new_extra), db_id))

        with self._lock:
            if index < len(self._items):
                self._items[index].extra = new_extra

        return {
            "verified": bool(verified),
            "verified_by": username,
            "verified_at": verified_at,
            "verify_history": history,
        }

    def invalidate(self) -> None:
        """Buộc load() tiếp theo phải query lại DB."""
        with self._lock:
            self._signature = None

    # ─── Internal helper ───────────────────────────────────────────────────────

    def _items_as_rows(self) -> list[dict]:
        """Xuất items hiện tại thành plain dicts cho ingest.merge."""
        return [
            item.extra | {
                "image": item.image,
                "post_id": item.post_id,
                "post_link": item.post_link,
                "caption": item.caption,
                "ground_truth": item.ground_truth,
                "phonetic": getattr(item, "phonetic", ""),
            }
            for item in self.items
        ]
