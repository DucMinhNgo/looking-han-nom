"""End to end over HTTP: sign in, search, open a picture."""

import json

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


def login(client, username=ADMIN_PASSWORD and "root", password=ADMIN_PASSWORD):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestAuth:
    def test_everything_is_closed_without_a_session(self, client):
        assert client.get("/api/lookup").status_code == 401
        assert client.get("/api/lookup/stats").status_code == 401
        assert client.get("/img/sample_00.jpg").status_code == 401
        assert client.get("/").status_code == 401

    def test_signing_in_opens_it(self, client):
        login(client)
        assert client.get("/api/lookup").status_code == 200

    def test_healthz_needs_no_session(self, client):
        assert client.get("/healthz").status_code == 200


class TestSearch:
    def test_an_empty_query_lists_everything(self, client):
        login(client)
        body = client.get("/api/lookup").json()
        assert body["total"] == 10
        assert len(body["items"]) == 10

    def test_by_caption(self, client):
        login(client)
        assert client.get("/api/lookup", params={"q": "Chú thích số 3"}).json()["total"] == 1

    def test_by_cjk_substring(self, client):
        login(client)
        assert client.get("/api/lookup", params={"q": "落又一"}).json()["total"] == 1

    def test_by_bare_post_number(self, client):
        login(client)
        body = client.get("/api/lookup", params={"q": "27835489826093105"}).json()
        assert body["total"] == 1

    def test_by_whole_facebook_link(self, client):
        login(client)
        body = client.get("/api/lookup", params={"q": URL}).json()
        assert body["total"] == 1

    def test_field_filter(self, client):
        login(client)
        assert client.get(
            "/api/lookup", params={"q": "Chú thích", "field": "ground_truth"}
        ).json()["total"] == 0

    def test_paging(self, client):
        login(client)
        first = client.get("/api/lookup", params={"limit": 4}).json()
        assert len(first["items"]) == 4 and first["has_more"] is True
        last = client.get("/api/lookup", params={"limit": 4, "offset": 8}).json()
        assert len(last["items"]) == 2 and last["has_more"] is False

    def test_a_result_carries_what_the_card_shows(self, client):
        login(client)
        item = client.get("/api/lookup", params={"q": "落又一"}).json()["items"][0]
        assert item["ground_truth"] == POEM
        assert item["caption"].startswith("Chú thích")
        assert item["post_url"].startswith("https://www.facebook.com/")
        assert item["has_image"] is True


class TestMismatch:
    def test_the_three_buckets_are_reported(self, client):
        login(client)
        images = client.get("/api/lookup/stats").json()["images"]
        assert images["matched"] == 9
        assert images["rows_without_image"] == 1
        assert images["orphan_images"] == 1
        assert images["files"] == 10

    def test_a_row_without_a_picture_is_still_searchable(self, client):
        """It is still readable text — only the picture is missing."""
        login(client)
        body = client.get("/api/lookup", params={"q": "Chú thích số 9"}).json()
        assert body["total"] == 1
        assert body["items"][0]["has_image"] is False

    def test_missing_only_narrows_to_them(self, client):
        login(client)
        body = client.get("/api/lookup", params={"missing_only": True}).json()
        assert body["total"] == 1
        assert all(not i["has_image"] for i in body["items"])

    def test_orphans_are_listable(self, client):
        login(client)
        body = client.get("/api/lookup/orphans").json()
        assert body["total"] == 1 and body["items"] == ["orphan.jpg"]

    def test_rescan_picks_up_a_new_file(self, client, tmp_path):
        login(client)
        (tmp_path / "images" / "sample_09.jpg").write_bytes(b"\xff\xd8late")
        body = client.post("/api/lookup/rescan").json()
        assert body["images"]["matched"] == 10
        assert body["images"]["rows_without_image"] == 0


class TestImages:
    def test_serves_a_picture(self, client):
        login(client)
        response = client.get("/img/sample_00.jpg")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content.startswith(b"\xff\xd8")

    def test_a_missing_picture_is_a_404(self, client):
        login(client)
        assert client.get("/img/sample_09.jpg").status_code == 404

    @pytest.mark.parametrize("name", ["../dataset.jsonl", "../../etc/passwd"])
    def test_traversal_is_refused(self, client, name):
        login(client)
        assert client.get(f"/img/{name}").status_code in (404, 400)


class TestStats:
    def test_dataset_counts(self, client):
        login(client)
        stats = client.get("/api/lookup/stats").json()["dataset"]
        assert stats["rows"] == 10
        assert stats["with_post_url"] == 10
        assert stats["posts"] == 10


class TestFirstRun:
    """The path the README promises: clone, one command, it runs."""

    def test_init_env_writes_a_file_that_starts_the_app(self, tmp_path, monkeypatch):
        from app.api.auth import AuthConfig
        from app.cli import cmd_init_env
        from app.core.config import load_env_file

        monkeypatch.chdir(tmp_path)
        assert cmd_init_env("a-good-password", force=False) == 0

        env = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "AUTH_SECRET=" in env and "APP_PASSWORD_HASH=" in env
        # Plain HTTP by default: a Secure cookie is never sent over http, and a
        # login that silently fails is the worst first impression there is.
        assert "COOKIE_SECURE=0" in env

        for key in ("AUTH_SECRET", "APP_PASSWORD_HASH", "APP_USERNAME"):
            monkeypatch.delenv(key, raising=False)
        assert load_env_file(tmp_path / ".env") is True

        # The generated secrets satisfy the startup check that refuses to serve.
        AuthConfig.from_env().validate()

    def test_the_generated_password_actually_signs_in(self, tmp_path, monkeypatch):
        from app.api.auth import AuthConfig
        from app.cli import cmd_init_env
        from app.core.config import load_env_file
        from app.core.users import UserStore

        monkeypatch.chdir(tmp_path)
        cmd_init_env("a-good-password", force=False)
        for key in ("AUTH_SECRET", "APP_PASSWORD_HASH", "APP_USERNAME"):
            monkeypatch.delenv(key, raising=False)
        load_env_file(tmp_path / ".env")

        auth = AuthConfig.from_env()
        store = UserStore(tmp_path / "users.json", super_admin="admin")
        assert auth.authenticate("admin", "a-good-password", store) is not None
        assert auth.authenticate("admin", "wrong", store) is None

    def test_it_refuses_to_clobber_an_existing_env(self, tmp_path, monkeypatch):
        from app.cli import cmd_init_env

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("KEEP=me\n", encoding="utf-8")
        assert cmd_init_env("a-good-password", force=False) == 1
        assert (tmp_path / ".env").read_text(encoding="utf-8") == "KEEP=me\n"
        assert cmd_init_env("a-good-password", force=True) == 0

    def test_a_short_password_is_refused(self, tmp_path, monkeypatch):
        from app.cli import cmd_init_env

        monkeypatch.chdir(tmp_path)
        assert cmd_init_env("short", force=False) == 1
        assert not (tmp_path / ".env").exists()

    def test_a_real_environment_variable_beats_the_file(self, tmp_path, monkeypatch):
        """In Docker the compose file is the truth; a stale .env must not win."""
        from app.core.config import load_env_file

        (tmp_path / ".env").write_text("APP_USERNAME=from-file\n", encoding="utf-8")
        monkeypatch.setenv("APP_USERNAME", "from-environment")
        load_env_file(tmp_path / ".env")
        import os

        assert os.environ["APP_USERNAME"] == "from-environment"

    def test_a_missing_env_file_is_not_an_error(self, tmp_path):
        from app.core.config import load_env_file

        assert load_env_file(tmp_path / "nope.env") is False
