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
from app.core.localimages import ImageLibrary, MatchReport
from app.core.users import UserStore

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.dataset = Dataset(settings.dataset_path)
        self.images = ImageLibrary(settings.images_dir)
        self.users = UserStore(
            settings.users_path,
            super_admin=os.environ.get("APP_USERNAME", "admin").strip().lower(),
        )
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
