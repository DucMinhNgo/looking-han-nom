"""Shared fixtures: a running app over a small, deliberately imperfect dataset."""

import io
import json
import zipfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.core.users import hash_password  # noqa: E402

ADMIN_PASSWORD = "admin-password"
URL = "https://www.facebook.com/permalink.php?story_fbid=27835489826093100&id=100000593113258"
POEM = "花開花落又一秋\n時光匆匆似水流"


@pytest.fixture
def client(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    # Nine rows with a picture, one without, plus one picture with no row —
    # the same deliberate drift the shipped sample has.
    rows = []
    for n in range(10):
        name = f"sample_{n:02d}.jpg"
        if n < 9:
            (images / name).write_bytes(b"\xff\xd8fake-jpeg")
        rows.append({
            "image": name,
            "post_id": URL.replace("093100", f"09{3100 + n}"),
            "caption": f"Chú thích số {n}",
            "ground_truth": POEM if n == 0 else f"文字{n}",
        })
    (images / "orphan.jpg").write_bytes(b"\xff\xd8fake-jpeg")

    (tmp_path / "dataset.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )

    monkeypatch.setenv("AUTH_SECRET", "t" * 48)
    monkeypatch.setenv("APP_USERNAME", "root")
    monkeypatch.setenv("APP_PASSWORD_HASH", hash_password(ADMIN_PASSWORD))
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATASET_PATH", str(tmp_path / "dataset.jsonl"))
    monkeypatch.setenv("IMAGES_DIR", str(images))

    from app.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def login(client, username="root", password=ADMIN_PASSWORD):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def jsonl_bytes(rows):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode()


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()
