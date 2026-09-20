#!/usr/bin/env python3
"""Import tài khoản từ users.json vào bảng users PostgreSQL — idempotent.

Cách dùng
---------
    # Từ trong container
    docker compose exec lookup python scripts/import_users.py

    # Chỉ định file và DB URL tùy chỉnh
    python scripts/import_users.py \\
        --users-file /srv/src/users.json \\
        --db-url postgresql://hannom:secret@localhost:5432/hannom

    # Dry-run (chỉ đọc, không ghi DB)
    python scripts/import_users.py --dry-run

    # Xem trợ giúp
    python scripts/import_users.py --help

Idempotent
----------
Dùng ``INSERT ... ON CONFLICT (username) DO NOTHING``.
Chạy nhiều lần không bị duplicate.

Super admin
-----------
Super admin (APP_USERNAME / APP_PASSWORD_HASH) không lưu trong DB.
Script bỏ qua dòng nào trùng với APP_USERNAME.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

log = logging.getLogger("import_users")


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
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
    host     = os.environ.get("POSTGRES_HOST", "postgres")
    port     = os.environ.get("POSTGRES_PORT", "5432")
    db       = os.environ.get("POSTGRES_DB",   "hannom")
    user     = os.environ.get("POSTGRES_USER", "hannom")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import tài khoản từ users.json vào PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--users-file",
        default="",
        metavar="PATH",
        help="Đường dẫn tới users.json (mặc định: DATA_DIR/users.json hoặc /srv/src/users.json)",
    )
    parser.add_argument(
        "--db-url",
        default="",
        metavar="URL",
        help="DATABASE_URL (mặc định: lấy từ env / .env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Đọc file và hiển thị sẽ import gì, không ghi vào DB",
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

    # Tìm users.json
    if args.users_file:
        json_path = Path(args.users_file)
    else:
        candidates = [
            Path(os.environ.get("USERS_PATH", "")),
            Path("/srv/src/users.json"),
            Path("data/users.json"),
            _ROOT / "data" / "users.json",
        ]
        json_path = next((p for p in candidates if p and p.is_file()), None)

    if not json_path or not json_path.is_file():
        log.error(
            "Không tìm thấy users.json. Chỉ định bằng --users-file hoặc "
            "đặt biến USERS_PATH."
        )
        sys.exit(1)

    log.info("Đọc %s …", json_path)
    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Không đọc được %s: %s", json_path, exc)
        sys.exit(1)

    if not isinstance(data, dict):
        log.error("users.json phải là một JSON object {username: {...}}")
        sys.exit(1)

    super_admin = os.environ.get("APP_USERNAME", "admin").strip().lower()
    log.info("Super admin (bỏ qua): %r", super_admin)

    # Hiển thị danh sách
    rows_to_import = [
        (name, row) for name, row in data.items()
        if isinstance(row, dict) and name.strip().lower() != super_admin
    ]

    log.info("Tìm thấy %d tài khoản cần import:", len(rows_to_import))
    for name, row in rows_to_import:
        log.info("  %-30s role=%-10s active=%s", name, row.get("role", "?"), row.get("active", True))

    if args.dry_run:
        log.info("🔍 DRY-RUN — không ghi vào DB.")
        return

    db_url = args.db_url or get_database_url()
    log.info("Database: %s", db_url.split("@")[-1])

    from app.core.pg_users import PgUserStore
    store = PgUserStore(db_url, super_admin=super_admin)

    imported = store.migrate_from_json(json_path)

    skipped = len(rows_to_import) - imported
    print(f"\n{'=' * 40}")
    print(f"  ✅ Imported  : {imported}")
    print(f"  ⏭  Skipped   : {skipped}  (đã có trong DB)")
    print(f"{'=' * 40}")

    if imported == 0 and skipped == 0:
        log.info("Không có tài khoản nào để import.")


if __name__ == "__main__":
    main()
