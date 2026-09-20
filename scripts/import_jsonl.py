#!/usr/bin/env python3
"""Import JSONL dataset vào PostgreSQL — idempotent, chạy được nhiều lần.

Cách dùng
---------
    # Import một file
    python scripts/import_jsonl.py /srv/src/dataset.jsonl

    # Import nhiều files (glob được xử lý bởi shell)
    python scripts/import_jsonl.py /srv/src/*.jsonl

    # Xem trợ giúp
    python scripts/import_jsonl.py --help

Chiến lược dedup
----------------
- Row **có** ``image``:  UNIQUE trên cột ``image``
  → INSERT nếu chưa có, UPDATE nếu đã có và nội dung khác nhau,
     bỏ qua nếu giống hệt (SKIP).
- Row **không có** ``image`` nhưng có ``post_id``:
  UNIQUE trên ``post_id`` (với image='').
- Row không có cả hai: luôn INSERT (không thể dedup được).

Biến môi trường
---------------
  DATABASE_URL  postgresql://user:pass@host:5432/dbname
               (mặc định: lấy từ .env rồi từ biến POSTGRES_* riêng lẻ)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── Thêm project root vào path để import app.core ────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

log = logging.getLogger("import_jsonl")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: đọc JSONL
# ─────────────────────────────────────────────────────────────────────────────

# Alias fields — giống logic trong app/core/dataset.py
_ALIASES: dict[str, tuple[str, ...]] = {
    "image":        ("image", "img", "image_name", "filename", "file"),
    "post_id":      ("post_id", "post_url", "post_link", "url", "link", "postid"),
    "caption":      ("caption", "fb_caption", "fb caption", "sub_caption"),
    "ground_truth": ("ground_truth", "groundtruth", "ground truth", "gt", "label"),
}


def _pick(row: dict[str, Any], field: str) -> str:
    for alias in _ALIASES[field]:
        value = row.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _pick_url(row: dict[str, Any]) -> str:
    """URL đầu tiên trong các alias của post_id."""
    for alias in _ALIASES["post_id"]:
        value = str(row.get(alias) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _consumed(row: dict[str, Any]) -> set[str]:
    used: set[str] = set()
    for field in _ALIASES:
        for alias in _ALIASES[field]:
            if row.get(alias) not in (None, ""):
                used.add(alias)
                break
    return used


def parse_file(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Đọc một file JSONL, trả về (rows, số_dòng_lỗi)."""
    rows: list[dict[str, Any]] = []
    malformed = 0
    with open(path, encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("%s:%d — JSON error: %s", path.name, lineno, exc)
                malformed += 1
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
            else:
                log.warning("%s:%d — expected object, got %s", path.name, lineno, type(parsed).__name__)
                malformed += 1
    return rows, malformed


def normalize_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Chuẩn hóa một row JSONL thành dict phù hợp với schema DB."""
    image        = _pick(raw, "image")
    post_id      = _pick(raw, "post_id")
    post_link    = _pick_url(raw)
    caption      = _pick(raw, "caption")
    ground_truth = _pick(raw, "ground_truth")
    extra        = {k: v for k, v in raw.items() if k not in _consumed(raw)}
    # Assign group_id from GROUP_TAGS_JSON env if image path matches
    try:
        group_tags_raw = os.environ.get("GROUP_TAGS_JSON", "")
        if group_tags_raw:
            group_tags = json.loads(group_tags_raw)
            image_path = (image or "").replace("\\", "/")
            for name in sorted(group_tags, key=len, reverse=True):
                if f"/{name}/" in f"/{image_path.lstrip('/')}":
                    extra.setdefault("group_id", group_tags[name])
                    extra.setdefault("group", name)
                    break
    except Exception:
        pass

    return {
        "image":        image,
        "post_id":      post_id,
        "post_link":    post_link,
        "caption":      caption,
        "ground_truth": ground_truth,
        "extra":        extra,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_database_url() -> str:
    """Lấy DATABASE_URL từ môi trường hoặc tổng hợp từ POSTGRES_* vars."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url

    # Thử load .env nếu có
    env_file = _ROOT / ".env"
    if env_file.is_file():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
            url = os.environ.get("DATABASE_URL", "").strip()
            if url:
                return url
        except ImportError:
            pass

    # Ghép từ biến riêng lẻ
    host     = os.environ.get("POSTGRES_HOST", "postgres")
    port     = os.environ.get("POSTGRES_PORT", "5432")
    db       = os.environ.get("POSTGRES_DB",   "hannom")
    user     = os.environ.get("POSTGRES_USER", "hannom")
    password = os.environ.get("POSTGRES_PASSWORD", "")

    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def connect(url: str):
    """Mở kết nối psycopg (psycopg3)."""
    try:
        import psycopg  # psycopg v3
    except ImportError:
        try:
            import psycopg2 as psycopg  # fallback psycopg2
        except ImportError:
            log.error(
                "Thiếu driver PostgreSQL. Cài đặt: pip install 'psycopg[binary]'"
            )
            sys.exit(1)

    return psycopg.connect(url)


# ─────────────────────────────────────────────────────────────────────────────
# Import logic
# ─────────────────────────────────────────────────────────────────────────────

_SQL_UPSERT_WITH_IMAGE = """
INSERT INTO dataset_items
    (image, post_id, post_link, caption, ground_truth, extra)
VALUES
    (%(image)s, %(post_id)s, %(post_link)s,
     %(caption)s, %(ground_truth)s, %(extra)s)
ON CONFLICT (image)
WHERE image <> ''
DO UPDATE SET
    post_id      = EXCLUDED.post_id,
    post_link    = EXCLUDED.post_link,
    caption      = EXCLUDED.caption,
    ground_truth = EXCLUDED.ground_truth,
    extra        = EXCLUDED.extra,
    updated_at   = NOW()
WHERE
    dataset_items.post_id      IS DISTINCT FROM EXCLUDED.post_id      OR
    dataset_items.post_link    IS DISTINCT FROM EXCLUDED.post_link    OR
    dataset_items.caption      IS DISTINCT FROM EXCLUDED.caption      OR
    dataset_items.ground_truth IS DISTINCT FROM EXCLUDED.ground_truth OR
    dataset_items.extra        IS DISTINCT FROM EXCLUDED.extra
RETURNING xmax::text::int = 0 AS inserted;
"""

_SQL_UPSERT_NO_IMAGE = """
INSERT INTO dataset_items
    (image, post_id, post_link, caption, ground_truth, extra)
VALUES
    ('', %(post_id)s, %(post_link)s,
     %(caption)s, %(ground_truth)s, %(extra)s)
ON CONFLICT (post_id)
WHERE image = '' AND post_id <> ''
DO UPDATE SET
    post_link    = EXCLUDED.post_link,
    caption      = EXCLUDED.caption,
    ground_truth = EXCLUDED.ground_truth,
    extra        = EXCLUDED.extra,
    updated_at   = NOW()
WHERE
    dataset_items.post_link    IS DISTINCT FROM EXCLUDED.post_link    OR
    dataset_items.caption      IS DISTINCT FROM EXCLUDED.caption      OR
    dataset_items.ground_truth IS DISTINCT FROM EXCLUDED.ground_truth OR
    dataset_items.extra        IS DISTINCT FROM EXCLUDED.extra
RETURNING xmax::text::int = 0 AS inserted;
"""

_SQL_INSERT_NO_KEY = """
INSERT INTO dataset_items
    (image, post_id, post_link, caption, ground_truth, extra)
VALUES
    ('', '', %(post_link)s, %(caption)s, %(ground_truth)s, %(extra)s)
RETURNING id;
"""


class Stats:
    def __init__(self) -> None:
        self.inserted = 0
        self.updated  = 0
        self.skipped  = 0
        self.no_key   = 0
        self.errors   = 0

    def report(self) -> str:
        total = self.inserted + self.updated + self.skipped + self.no_key
        return (
            f"  Tổng xử lý : {total}\n"
            f"  ✅ Inserted  : {self.inserted}\n"
            f"  🔄 Updated   : {self.updated}\n"
            f"  ⏭  Skipped   : {self.skipped}\n"
            f"  ⚠️  No key    : {self.no_key}  (không có image lẫn post_id — vẫn insert)\n"
            f"  ❌ Errors    : {self.errors}"
        )


def import_rows(
    conn,
    rows: list[dict[str, Any]],
    stats: Stats,
    dry_run: bool = False,
) -> None:
    """Insert / update toàn bộ rows vào DB trong một transaction."""
    if dry_run:
        # Không cần driver DB trong dry-run — chỉ đếm
        for row in rows:
            norm = normalize_row(row)
            if norm["image"] or norm["post_id"]:
                stats.inserted += 1
            else:
                stats.no_key += 1
                stats.inserted += 1
        return

    try:
        import psycopg
        use_psycopg3 = True
    except ImportError:
        try:
            import psycopg2 as psycopg
        except ImportError:
            log.error(
                "Thiếu driver PostgreSQL. Cài đặt: pip install 'psycopg[binary]'"
            )
            import sys; sys.exit(1)
        use_psycopg3 = False

    with conn.cursor() as cur:
        for row in rows:
            norm = normalize_row(row)

            # psycopg3 dùng json.dumps; psycopg2 dùng Json() adapter
            if use_psycopg3:
                extra_val = json.dumps(norm["extra"], ensure_ascii=False)
            else:
                from psycopg2.extras import Json
                extra_val = Json(norm["extra"])

            params = {**norm, "extra": extra_val}

            try:
                if norm["image"]:
                    sql = _SQL_UPSERT_WITH_IMAGE
                elif norm["post_id"]:
                    sql = _SQL_UPSERT_NO_IMAGE
                else:
                    # Không có key — insert mù
                    if not dry_run:
                        cur.execute(_SQL_INSERT_NO_KEY, params)
                    stats.no_key += 1
                    stats.inserted += 1
                    continue

                if dry_run:
                    stats.inserted += 1  # giả định
                    continue

                cur.execute(sql, params)
                result = cur.fetchone()
                if result is None:
                    # DO NOTHING khi ON CONFLICT không match WHERE clause → skipped
                    stats.skipped += 1
                elif result[0]:  # inserted = True (xmax == 0)
                    stats.inserted += 1
                else:             # updated
                    stats.updated += 1

            except Exception as exc:
                log.error("Lỗi khi insert row (image=%r): %s", norm.get("image"), exc)
                stats.errors += 1
                conn.rollback()
                return

        if not dry_run:
            conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import JSONL dataset vào PostgreSQL — idempotent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="Một hoặc nhiều file .jsonl cần import",
    )
    parser.add_argument(
        "--db-url",
        metavar="URL",
        default="",
        help="DATABASE_URL (mặc định: lấy từ env / .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Phân tích files nhưng không ghi vào DB",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Log chi tiết hơn",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    db_url = args.db_url or get_database_url()
    log.info("Database URL: %s", db_url.split("@")[-1])  # ẩn password

    if args.dry_run:
        log.info("🔍 DRY-RUN mode — không ghi vào DB")
        conn = None
    else:
        log.info("Đang kết nối PostgreSQL…")
        conn = connect(db_url)
        log.info("✅ Kết nối thành công")

    total_stats = Stats()

    for file_arg in args.files:
        path = Path(file_arg)
        if not path.is_file():
            log.warning("Không tìm thấy file: %s — bỏ qua", path)
            continue

        log.info("📄 Đọc %s …", path)
        rows, malformed = parse_file(path)
        log.info("   %d rows đọc được, %d dòng lỗi", len(rows), malformed)

        file_stats = Stats()
        file_stats.errors += malformed

        if conn is not None or args.dry_run:
            import_rows(conn, rows, file_stats, dry_run=args.dry_run)

        log.info("Kết quả cho %s:\n%s", path.name, file_stats.report())

        total_stats.inserted += file_stats.inserted
        total_stats.updated  += file_stats.updated
        total_stats.skipped  += file_stats.skipped
        total_stats.no_key   += file_stats.no_key
        total_stats.errors   += file_stats.errors

    if len(args.files) > 1:
        print("\n=== TỔNG KẾT ===")
        print(total_stats.report())

    if conn is not None:
        conn.close()

    if total_stats.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
