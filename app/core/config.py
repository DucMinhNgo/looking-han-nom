"""Env-driven settings (12-factor). No config files, no secrets in code.

Two paths and the login. Everything the lookup tool needs is mounted from the
host, so the same image runs locally and on the VPS with only the env changing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ENV_FILE = ".env"


def load_env_file(path: str | Path = ENV_FILE) -> bool:
    """Read a ``.env`` into the environment, if one is there.

    Called explicitly by the entry points rather than on import, so nothing
    happens behind a test's back. Real environment variables always win — in
    Docker the compose file is the source of truth and a stale ``.env`` copied
    from a laptop must not override it.

    python-dotenv comes with ``uvicorn[standard]``, so this costs no dependency.
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:  # pragma: no cover - ships with uvicorn[standard]
        return False
    load_dotenv(path, override=False)
    log.info("loaded %s", path)
    return True


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("/data")
    dataset_path: Path = Path("/data/dataset.jsonl")
    images_dir: Path = Path("/data/images")
    page_size: int = 24

    @property
    def users_path(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def backups_dir(self) -> Path:
        """Previous versions of the dataset.

        Under DATA_DIR rather than beside the dataset: the source folder may be
        mounted read-only, and a backup that cannot be written is not a backup.
        """
        return self.data_dir / "backups"


def load_settings() -> Settings:
    """Build settings from the environment. Never logs secret values."""
    data_dir = Path(_env("DATA_DIR", "/data"))
    return Settings(
        data_dir=data_dir,
        dataset_path=Path(_env("DATASET_PATH", str(data_dir / "dataset.jsonl"))),
        images_dir=Path(_env("IMAGES_DIR", str(data_dir / "images"))),
        page_size=_env_int("PAGE_SIZE", 24),
    )


def describe_secrets() -> dict[str, bool]:
    """Startup diagnostics: which secrets are PRESENT. Never their values."""
    return {
        "AUTH_SECRET": bool(_env("AUTH_SECRET")),
        "APP_PASSWORD_HASH": bool(_env("APP_PASSWORD_HASH")),
    }


def password_hash_complaint() -> str:
    """Why the configured hash cannot work, or "" if it looks usable.

    Worth its own check because the failure it catches is invisible otherwise:
    Docker Compose interpolates ``$NAME`` inside an env_file, so an unquoted
    ``$2b$12$<salt><hash>`` arrives with the salt eaten. The app then starts
    happily and simply rejects the right password forever.
    """
    value = _env("APP_PASSWORD_HASH")
    if not value:
        return ""
    if not value.startswith("$2"):
        return "APP_PASSWORD_HASH is not a bcrypt hash (it should start with $2b$)."
    if len(value) < 59:
        return (
            "APP_PASSWORD_HASH is truncated — it should be 60 characters, this "
            f"one is {len(value)}. In Docker this is almost always an unquoted "
            "hash in .env: Compose read $2b$12$SALT as a variable and dropped "
            "the salt. Wrap the value in single quotes and recreate the "
            "container."
        )
    return ""
