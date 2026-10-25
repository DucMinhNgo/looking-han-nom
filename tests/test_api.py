"""End to end over HTTP: sign in, search, open a picture.

The app fixture and its helpers live in conftest.py.
"""

import json
from datetime import datetime, timezone

import pytest

from tests.conftest import POEM, URL, login  # noqa: F401
from app.core.dataset import Dataset
from app.core.pg_dataset import PgDataset


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

    def test_han_similarity_filter_applies_before_pagination(self, client):
        login(client)
        path = client.app.state.runtime.dataset.path
        rows = [
            {"image": "a.jpg", "caption": "花開花落", "ground_truth": "花開花落"},
            {"image": "b.jpg", "caption": "花開花落", "ground_truth": "天地玄黃"},
            {"image": "c.jpg", "caption": "花開花落", "ground_truth": "花開花落"},
        ]
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        body = client.get("/api/lookup", params={
            "min_han_similarity": 80, "offset": 1, "limit": 1,
        }).json()
        assert body["total"] == 2
        assert len(body["items"]) == 1
        assert body["items"][0]["image"] == "c.jpg"

    def test_han_similarity_filter_excludes_rows_without_han_text(self, client):
        login(client)
        body = client.get("/api/lookup", params={"min_han_similarity": 80}).json()
        assert body["total"] == 0

    def test_timestamp_sort_requires_database_backend(self, client):
        login(client)
        runtime = client.app.state.runtime
        runtime.dataset = Dataset(runtime.settings.dataset_path)

        response = client.get("/api/lookup", params={"sort": "created_desc"})
        assert response.status_code == 503

    def test_timestamp_sort_uses_database_columns(self, client):
        login(client)
        runtime = client.app.state.runtime
        dataset = PgDataset("postgresql://unused")
        newest = datetime(2025, 1, 3, tzinfo=timezone.utc)
        oldest = datetime(2025, 1, 1, tzinfo=timezone.utc)
        dataset._execute = lambda sql, params=(): (
            [(newest,)] if sql == "SELECT MAX(updated_at) FROM dataset_items" else [
                (1, "db_oldest.jpg", "p1", "", "", "", "", {}, oldest, oldest),
                (2, "db_newest.jpg", "p2", "", "", "", "", {}, newest, newest),
            ]
        )
        runtime.dataset = dataset

        response = client.get("/api/lookup", params={
            "sort": "created_desc", "limit": 1,
        })
        assert response.status_code == 200
        assert response.json()["items"][0]["image"] == "db_newest.jpg"

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


class TestQwenFontSelection:
    def test_font_selection_requires_configured_qwen(self, client):
        login(client)

        response = client.post(
            "/api/qwen/select-font",
            json={"index": 0, "font_keys": ["thao"]},
        )

        assert response.status_code == 200
        assert response.json()["enabled"] is False

    def test_qwen_selects_only_from_requested_font_groups(self, client, monkeypatch):
        from types import SimpleNamespace
        import openai
        from app.api import routes_qwen

        login(client)
        client.app.state.settings.qwen_api_key = "test-key"
        routes_qwen._font_choice_cache.clear()
        calls = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create_completion),
                )

            def create_completion(self, **kwargs):
                calls.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content='{"font_key":"thao","reason":"Thảo style"}',
                    ))],
                    usage=SimpleNamespace(prompt_tokens=120, completion_tokens=18),
                )

        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
        response = client.post(
            "/api/qwen/select-font",
            json={"index": 0, "font_keys": ["thao"]},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["font_key"] == "thao"
        assert body["usage"] == {"input_tokens": 120, "output_tokens": 18}
        assert calls["model"] == client.app.state.settings.qwen_model

    def test_font_selection_can_compare_record_image(self, client, monkeypatch):
        from types import SimpleNamespace
        import openai
        from app.api import routes_qwen

        login(client)
        client.app.state.settings.qwen_api_key = "test-key"
        routes_qwen._font_choice_cache.clear()
        calls = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create_completion),
                )

            def create_completion(self, **kwargs):
                calls.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content='{"font_key":"thao","reason":"matches image"}',
                    ))],
                    usage=None,
                )

        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
        response = client.post(
            "/api/qwen/select-font",
            json={"index": 0, "font_keys": ["thao"], "include_image": True},
        )

        assert response.status_code == 200, response.text
        content = calls["messages"][0]["content"]
        assert any(part["type"] == "image_url" for part in content)
        assert "attached image" in content[0]["text"]

    def test_all_font_groups_return_per_character_assignments(self, client, monkeypatch):
        from types import SimpleNamespace
        import openai

        login(client)
        client.app.state.settings.qwen_api_key = "test-key"
        calls = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create_completion),
                )

            def create_completion(self, **kwargs):
                calls.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content='{"fonts":[{"char":"花","font_key":"thao"},{"char":"開","font_key":"trien"}],"reason":"best coverage"}',
                    ))],
                    usage=None,
                )

        monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
        response = client.post(
            "/api/qwen/select-font",
            json={
                "index": 0,
                "font_keys": ["thao", "trien"],
                "per_character": True,
                "preferred_font_key": "thao",
                "force_refresh": True,
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["font_map"] == {"花": "thao", "開": "trien"}
        prompt = calls["messages"][0]["content"]
        assert "thao" in prompt



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
