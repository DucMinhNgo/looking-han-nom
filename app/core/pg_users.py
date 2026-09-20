"""PostgreSQL-backed user store — drop-in thay thế cho UserStore (file-based).

Cùng interface với ``app.core.users.UserStore``:
  get(), list(), reviewer_names(),
  create(), set_password(), rename(), delete(), set_active(),
  record_login(), authenticate()

Cách dùng
---------
Khi ``DATABASE_URL`` được set, ``Runtime`` sẽ dùng ``PgUserStore`` thay vì
``UserStore``.  Nếu DB không sẵn sàng, app fallback về file JSON.

Super admin
-----------
Super admin vẫn đến từ env (``APP_USERNAME`` / ``APP_PASSWORD_HASH``) và không
bao giờ được ghi vào bảng ``users`` — đây là cơ chế an toàn: mất DB không mất
quyền admin.

Migration
---------
Nếu ``users.json`` đã tồn tại khi PgUserStore khởi động lần đầu, nó sẽ tự
động import tất cả tài khoản vào DB (idempotent — dùng ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.users import (
    ROLE_ADMIN,
    ROLE_REVIEWER,
    ROLES,
    MIN_PASSWORD_LEN,
    USERNAME_RE,
    User,
    UserError,
    hash_password,
    verify_password,
)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── SQL statements ────────────────────────────────────────────────────────────

_SQL_GET = """
SELECT username, role, password_hash, display_name, active,
       created_at, created_by, last_login_at
  FROM users WHERE username = %s
"""

_SQL_LIST = """
SELECT username, role, display_name, active, created_at, created_by, last_login_at
  FROM users ORDER BY username
"""

_SQL_INSERT = """
INSERT INTO users
    (username, role, password_hash, display_name, active, created_by)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (username) DO NOTHING
RETURNING username
"""

_SQL_UPDATE_PASSWORD = """
UPDATE users SET password_hash = %s WHERE username = %s
"""

_SQL_RENAME = """
UPDATE users SET username = %s WHERE username = %s
"""

_SQL_DELETE = """
DELETE FROM users WHERE username = %s
"""

_SQL_SET_ACTIVE = """
UPDATE users SET active = %s WHERE username = %s
"""

_SQL_RECORD_LOGIN = """
UPDATE users SET last_login_at = NOW() WHERE username = %s
"""

_SQL_GET_HASH = """
SELECT password_hash, active FROM users WHERE username = %s
"""


# ─── Connection helper ─────────────────────────────────────────────────────────

def _connect(url: str):
    """Mở kết nối — thử psycopg3 trước, fallback psycopg2."""
    try:
        import psycopg
        return psycopg.connect(url, autocommit=False)
    except ImportError:
        pass
    try:
        import psycopg2
        return psycopg2.connect(url)
    except ImportError:
        raise RuntimeError(
            "Cần cài psycopg3 hoặc psycopg2: pip install 'psycopg[binary]'"
        )


# ─── PgUserStore ───────────────────────────────────────────────────────────────

class PgUserStore:
    """Tài khoản reviewer lưu trong PostgreSQL.

    Thread-safe: mỗi operation mở cursor riêng trong connection có sẵn.
    Dùng một connection duy nhất với lock — phù hợp với single-worker uvicorn.
    Nếu kết nối bị mất, tự reconnect.
    """

    def __init__(self, database_url: str, super_admin: str = "") -> None:
        self._url = database_url
        self.super_admin = super_admin.strip().lower()
        self._conn = None
        self._lock = threading.Lock()

    # ─── Connection management ─────────────────────────────────────────────────

    def _get_conn(self):
        """Trả về connection, reconnect nếu cần."""
        if self._conn is None:
            self._conn = _connect(self._url)
            return self._conn
        # Kiểm tra connection còn sống không
        try:
            self._conn.cursor().execute("SELECT 1")
            return self._conn
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = _connect(self._url)
            return self._conn

    def _execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Chạy một câu SQL và trả về kết quả (SELECT) hoặc [] (DML)."""
        with self._lock:
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

    # ─── Row → User ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: tuple) -> User:
        """Map một tuple DB row thành User object."""
        # Hỗ trợ cả 8-cột (get) và 7-cột (list, không có password_hash)
        if len(row) == 8:
            username, role, _hash, display_name, active, created_at, created_by, last_login_at = row
        else:
            username, role, display_name, active, created_at, created_by, last_login_at = row
        return User(
            username=str(username),
            role=str(role),
            display_name=str(display_name or ""),
            active=bool(active),
            created_at=created_at.isoformat(timespec="seconds") if hasattr(created_at, "isoformat") else str(created_at or ""),
            created_by=str(created_by or ""),
            last_login_at=last_login_at.isoformat(timespec="seconds") if last_login_at and hasattr(last_login_at, "isoformat") else str(last_login_at or ""),
        )

    # ─── Queries ───────────────────────────────────────────────────────────────

    def get(self, username: str) -> User | None:
        """Lấy user theo username, hoặc synthesize super admin."""
        name = (username or "").strip().lower()
        if not name:
            return None
        if name == self.super_admin:
            return User(
                username=name,
                role=ROLE_ADMIN,
                display_name="Super admin",
                created_by="environment",
            )
        try:
            rows = self._execute(_SQL_GET, (name,))
        except Exception as exc:
            log.error("PgUserStore.get(%r) failed: %s", name, exc)
            return None
        if not rows:
            return None
        return self._row_to_user(rows[0])

    def list(self) -> list[User]:
        """Tất cả tài khoản, super admin đứng đầu."""
        users: list[User] = []
        if self.super_admin:
            admin = self.get(self.super_admin)
            if admin is not None:
                users.append(admin)
        try:
            rows = self._execute(_SQL_LIST)
        except Exception as exc:
            log.error("PgUserStore.list() failed: %s", exc)
            return users
        for row in rows:
            u = self._row_to_user(row)
            if u.username != self.super_admin:
                users.append(u)
        return users

    def reviewer_names(self) -> list[str]:
        return [u.username for u in self.list() if u.active]

    # ─── Mutations ─────────────────────────────────────────────────────────────

    def create(
        self,
        username: str,
        password: str,
        *,
        role: str = ROLE_REVIEWER,
        display_name: str = "",
        created_by: str = "",
    ) -> User:
        name = (username or "").strip().lower()
        if not USERNAME_RE.match(name):
            raise UserError(
                "Username phải 3-32 ký tự, hoặc là email hợp lệ. "
                "Dùng chữ thường, số, dấu chấm, gạch ngang hoặc gạch dưới."
            )
        if role not in ROLES:
            raise UserError(f"Role phải là một trong: {', '.join(ROLES)}")
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Mật khẩu phải ít nhất {MIN_PASSWORD_LEN} ký tự.")
        if name == self.super_admin:
            raise UserError("Username đó là super admin, được đặt trong môi trường.")

        pw_hash = hash_password(password)
        try:
            rows = self._execute(
                _SQL_INSERT,
                (name, role, pw_hash, display_name.strip(), True, created_by),
            )
        except Exception as exc:
            raise UserError(f"Lỗi DB khi tạo user: {exc}") from exc

        if not rows:
            raise UserError(f"User {name!r} đã tồn tại.")

        log.info("created user %r (role=%s) by %r", name, role, created_by or "?")
        return User(
            username=name,
            role=role,
            display_name=display_name.strip(),
            created_by=created_by,
        )

    def set_password(self, username: str, password: str) -> None:
        name = (username or "").strip().lower()
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Mật khẩu phải ít nhất {MIN_PASSWORD_LEN} ký tự.")
        try:
            self._execute(_SQL_UPDATE_PASSWORD, (hash_password(password), name))
        except Exception as exc:
            raise UserError(f"Lỗi DB: {exc}") from exc
        log.info("password changed for %r", name)

    def rename(self, username: str, new_username: str) -> User:
        old_name = (username or "").strip().lower()
        new_name = (new_username or "").strip().lower()
        if old_name == self.super_admin:
            raise UserError("Không thể đổi tên super admin.")
        if not USERNAME_RE.match(new_name):
            raise UserError("Username mới không hợp lệ.")
        if new_name == self.super_admin:
            raise UserError("Username đó đã dành cho super admin.")
        try:
            self._execute(_SQL_RENAME, (new_name, old_name))
        except Exception as exc:
            # psycopg ném UniqueViolation nếu new_name đã tồn tại
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise UserError(f"User {new_name!r} đã tồn tại.") from exc
            raise UserError(f"Lỗi DB: {exc}") from exc
        log.info("renamed user %r to %r", old_name, new_name)
        result = self.get(new_name)
        if result is None:
            raise UserError(f"Không tìm thấy user {old_name!r} để đổi tên.")
        return result

    def delete(self, username: str) -> None:
        name = (username or "").strip().lower()
        if name == self.super_admin:
            raise UserError("Không thể xóa super admin.")
        try:
            self._execute(_SQL_DELETE, (name,))
        except Exception as exc:
            raise UserError(f"Lỗi DB: {exc}") from exc
        log.info("deleted user %r", name)

    def set_active(self, username: str, active: bool) -> None:
        name = (username or "").strip().lower()
        if name == self.super_admin:
            raise UserError("Không thể vô hiệu hóa super admin.")
        try:
            self._execute(_SQL_SET_ACTIVE, (bool(active), name))
        except Exception as exc:
            raise UserError(f"Lỗi DB: {exc}") from exc

    def record_login(self, username: str) -> None:
        name = (username or "").strip().lower()
        if name == self.super_admin:
            return
        try:
            self._execute(_SQL_RECORD_LOGIN, (name,))
        except Exception as exc:
            log.warning("record_login(%r) failed: %s", name, exc)

    # ─── Authentication ────────────────────────────────────────────────────────

    def authenticate(self, username: str, password: str) -> User | None:
        """Xác thực tài khoản được lưu trong DB. Super admin xác thực qua AuthConfig."""
        name = (username or "").strip().lower()
        try:
            rows = self._execute(_SQL_GET_HASH, (name,))
        except Exception as exc:
            log.error("authenticate(%r) DB error: %s", name, exc)
            # Spend time anyway để không lộ thông tin qua timing
            verify_password(password, "$2b$12$" + "." * 53)
            return None

        if not rows:
            verify_password(password, "$2b$12$" + "." * 53)
            return None

        pw_hash, active = rows[0]
        if not verify_password(password, pw_hash or ""):
            return None
        if not active:
            return None
        return self.get(name)

    # ─── Migration từ users.json ───────────────────────────────────────────────

    def migrate_from_json(self, json_path: Path) -> int:
        """Import tài khoản từ users.json vào DB.

        Idempotent — dùng ON CONFLICT DO NOTHING.
        Trả về số lượng tài khoản đã được import.
        """
        from app.core.jsonlog import read_json

        data = read_json(json_path, default={})
        if not isinstance(data, dict) or not data:
            return 0

        imported = 0
        for name, row in data.items():
            if not isinstance(row, dict):
                continue
            try:
                rows = self._execute(
                    _SQL_INSERT,
                    (
                        name.strip().lower(),
                        row.get("role", ROLE_REVIEWER),
                        row.get("password_hash", ""),
                        row.get("display_name", ""),
                        bool(row.get("active", True)),
                        row.get("created_by", ""),
                    ),
                )
                if rows:
                    imported += 1
            except Exception as exc:
                log.warning("migrate_from_json: skip %r — %s", name, exc)

        if imported:
            log.info("Migrated %d users from %s to PostgreSQL", imported, json_path)
        return imported
