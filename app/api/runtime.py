"""Process-wide objects the routes share.

One place to build the dataset, the image library and the user store, so the
routes stay thin and a test can point the whole thing at a temp directory.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.core.config import Settings
from app.core.dataset import Dataset
try:
    from app.core.pg_dataset import PgDataset
except Exception:
    PgDataset = None  # type: ignore
from app.core.localimages import ImageLibrary, MatchReport
from app.core.users import UserStore

log = logging.getLogger(__name__)


def _build_user_store(settings: Settings, super_admin: str) -> UserStore:
    """Trả về PgUserStore nếu DATABASE_URL có sẵn, ngược lại dùng file JSON.

    Khi PgUserStore được khởi tạo lần đầu và users.json cũ vẫn tồn tại,
    nó sẽ tự động migrate các tài khoản vào DB (idempotent).
    """
    db_url = settings.database_url
    if db_url:
        try:
            from app.core.pg_users import PgUserStore
            store = PgUserStore(db_url, super_admin=super_admin)
            # Kiểm tra kết nối ngay lập tức
            store.get(super_admin)
            log.info("Users: dùng PostgreSQL backend (%s)", db_url.split("@")[-1])

            # Tự động migrate từ users.json nếu file đó còn tồn tại
            json_path = settings.users_path
            if json_path.is_file():
                try:
                    count = store.migrate_from_json(json_path)
                    if count:
                        log.info(
                            "Migrated %d user(s) từ %s vào PostgreSQL",
                            count, json_path,
                        )
                except Exception as exc:
                    log.warning("Migration từ users.json thất bại: %s", exc)

            return store  # type: ignore[return-value]
        except Exception as exc:
            log.warning(
                "Không thể kết nối PostgreSQL cho UserStore (%s) — "
                "fallback về file JSON. Lỗi: %s",
                db_url.split("@")[-1], exc,
            )

    log.info("Users: dùng file JSON backend (%s)", settings.users_path)
    return UserStore(settings.users_path, super_admin=super_admin)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # If DATABASE_URL is set, PgDataset will be used (import above).
        # For the file-backed Dataset the constructor still takes a path.
        if settings.database_url and PgDataset is not None:
            try:
                self.dataset = PgDataset(settings.database_url)
            except Exception:
                # Fallback to file-backed dataset when PgDataset cannot be used
                log.warning("Could not init PgDataset, falling back to file Dataset")
                self.dataset = Dataset(settings.dataset_path)
        else:
            self.dataset = Dataset(settings.dataset_path)
        self.images = ImageLibrary(settings.images_dir)
        super_admin = os.environ.get("APP_USERNAME", "admin").strip().lower()
        self.users = _build_user_store(settings, super_admin)
        self._report: MatchReport | None = None

    def refresh(self, force: bool = False) -> MatchReport:
        """Re-resolve every row against the folder.

        Both sides are cached on their own change signature, so calling this on
        each request is cheap and means an edited dataset or a newly copied
        image appears without a restart.
        """
        if force:
            self.dataset.load(force=True)
            self.images.scan(force=True)
        self._report = self.images.attach(self.dataset.items)
        return self._report

    @property
    def report(self) -> MatchReport:
        return self.refresh()

    async def startup(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        report = self.refresh(force=True)
        log.info(
            "dataset %d rows | images %d files | matched %d | "
            "rows without an image %d | orphan images %d",
            len(self.dataset.items), report.scanned, report.matched,
            report.rows_without_image, report.orphans,
        )

    async def shutdown(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset.stats(),
            "images": self.report.to_json(),
            "page_size": self.settings.page_size,
        }
