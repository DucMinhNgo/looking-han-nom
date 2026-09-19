"""Growing the corpus over the web, without touching the server's filesystem."""

from tests.conftest import URL, jsonl_bytes, login, make_zip


class TestPermissions:
    def test_everything_here_is_admin_only(self, client):
        login(client)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")

        assert client.post(
            "/api/data/dataset/preview",
            files={"file": ("d.jsonl", jsonl_bytes([{"image": "x.jpg"}]))},
        ).status_code == 403
        assert client.post(
            "/api/data/images", files={"files": ("a.jpg", b"\xff\xd8")}
        ).status_code == 403
        assert client.get("/api/data/backups").status_code == 403

    def test_signed_out_is_refused(self, client):
        assert client.get("/api/data/backups").status_code == 401


class TestMergingADataset:
    def test_preview_reports_without_writing(self, client, tmp_path):
        login(client)
        before = (tmp_path / "dataset.jsonl").read_bytes()

        body = client.post(
            "/api/data/dataset/preview",
            files={"file": ("new.jsonl", jsonl_bytes([
                {"image": "sample_00.jpg", "caption": "đã sửa"},
                {"image": "brand_new.jpg", "caption": "mới"},
            ]))},
        ).json()

        assert body["added"] == 1 and body["updated"] == 1
        assert body["current_rows"] == 10 and body["total"] == 11
        assert (tmp_path / "dataset.jsonl").read_bytes() == before

    def test_merge_adds_without_losing_anything(self, client):
        login(client)
        body = client.post(
            "/api/data/dataset/merge",
            files={"file": ("new.jsonl", jsonl_bytes([
                {"image": "brand_new.jpg", "post_id": URL,
                 "caption": "mới", "ground_truth": "新"},
            ]))},
        ).json()

        assert body["added"] == 1 and body["total"] == 11
        # Searchable at once, and nothing that was there has gone.
        assert client.get("/api/lookup", params={"q": "新"}).json()["total"] == 1
        assert client.get("/api/lookup").json()["total"] == 11
        assert client.get("/api/lookup", params={"q": "落又一"}).json()["total"] == 1

    def test_an_existing_row_is_updated_in_place_not_duplicated(self, client):
        login(client)
        client.post(
            "/api/data/dataset/merge",
            files={"file": ("new.jsonl", jsonl_bytes([
                {"image": "sample_00.jpg", "post_id": URL,
                 "caption": "caption đã sửa", "ground_truth": "文字mới"},
            ]))},
        )
        found = client.get("/api/lookup", params={"q": "caption đã sửa"}).json()
        assert found["total"] == 1 and found["items"][0]["index"] == 0
        assert client.get("/api/lookup").json()["total"] == 10

    def test_an_unreadable_upload_is_refused_clearly(self, client):
        login(client)
        response = client.post(
            "/api/data/dataset/merge",
            files={"file": ("d.jsonl", b"this is not jsonl at all")},
        )
        assert response.status_code == 400
        assert "readable rows" in response.json()["detail"]

    def test_an_empty_upload_is_refused(self, client):
        login(client)
        assert client.post(
            "/api/data/dataset/merge", files={"file": ("d.jsonl", b"")}
        ).status_code == 400


class TestBackups:
    def test_the_previous_version_is_kept_and_downloadable(self, client):
        login(client)
        client.post(
            "/api/data/dataset/merge",
            files={"file": ("new.jsonl", jsonl_bytes([
                {"image": "n.jpg", "caption": "x"}]))},
        )
        backups = client.get("/api/data/backups").json()["items"]
        assert len(backups) == 1

        restored = client.get(f"/api/data/backups/{backups[0]['name']}")
        assert restored.status_code == 200
        # The corpus as it was before the merge: ten rows, not eleven.
        assert len(restored.text.strip().splitlines()) == 10

    def test_a_crafted_backup_name_cannot_escape_the_folder(self, client):
        login(client)
        assert client.get(
            "/api/data/backups/../users.json"
        ).status_code in (404, 400)


class TestUploadingPictures:
    def test_one_file(self, client):
        login(client)
        body = client.post(
            "/api/data/images",
            files=[("files", ("new_one.jpg", b"\xff\xd8new", "image/jpeg"))],
        ).json()
        assert body["saved"] == 1
        assert client.get("/img/new_one.jpg").status_code == 200
        # No row describes it yet, so it is reported as an orphan.
        assert body["images"]["orphan_images"] == 2

    def test_a_zip_of_them(self, client):
        login(client)
        data = make_zip([
            ("z1.jpg", b"\xff\xd8a"),
            ("nested/z2.png", b"\x89PNGb"),
            ("readme.txt", b"hi"),
        ])
        body = client.post(
            "/api/data/images",
            files=[("files", ("batch.zip", data, "application/zip"))],
        ).json()
        assert body["saved"] == 2 and body["skipped_not_image"] == 1
        # Flattened out of its folder, so the scan sees it immediately.
        assert client.get("/img/z2.png").status_code == 200

    def test_an_existing_picture_is_not_silently_replaced(self, client):
        login(client)
        original = client.get("/img/sample_00.jpg").content
        body = client.post(
            "/api/data/images",
            files=[("files", ("sample_00.jpg", b"\xff\xd8other", "image/jpeg"))],
        ).json()
        assert body["saved"] == 0 and body["skipped_conflict"] == 1
        assert client.get("/img/sample_00.jpg").content == original

    def test_a_zip_entry_cannot_escape_the_folder(self, client, tmp_path):
        login(client)
        data = make_zip([("../../escaped.jpg", b"\xff\xd8")])
        client.post(
            "/api/data/images",
            files=[("files", ("evil.zip", data, "application/zip"))],
        )
        assert not (tmp_path.parent / "escaped.jpg").exists()

    def test_adding_a_picture_turns_a_missing_row_into_a_match(self, client):
        """The point of the whole feature: the row was already waiting for it."""
        login(client)
        stats = client.get("/api/lookup/stats").json()["images"]
        assert stats["rows_without_image"] == 1

        client.post(
            "/api/data/images",
            files=[("files", ("sample_09.jpg", b"\xff\xd8late", "image/jpeg"))],
        )
        stats = client.get("/api/lookup/stats").json()["images"]
        assert stats["rows_without_image"] == 0 and stats["matched"] == 10


class TestAddingOneRow:
    def test_with_its_picture_attached(self, client):
        login(client)
        body = client.post(
            "/api/data/row",
            data={"post_id": URL, "caption": "nhập tay",
                  "ground_truth": "獨釣寒江雪"},
            files={"file": ("manual.jpg", b"\xff\xd8m", "image/jpeg")},
        ).json()

        assert body["added"] == 1 and body["total"] == 11
        found = client.get("/api/lookup", params={"q": "獨釣寒江雪"}).json()
        assert found["total"] == 1 and found["items"][0]["has_image"] is True

    def test_naming_a_picture_that_is_already_there(self, client):
        login(client)
        body = client.post(
            "/api/data/row",
            data={"image_name": "orphan.jpg", "caption": "giờ đã có dòng",
                  "ground_truth": "無主"},
        ).json()
        assert body["added"] == 1
        # The orphan has found its row.
        assert client.get("/api/lookup/stats").json()["images"]["orphan_images"] == 0

    def test_it_updates_rather_than_duplicates(self, client):
        login(client)
        client.post("/api/data/row", data={
            "image_name": "sample_00.jpg", "caption": "viết lại",
            "ground_truth": "文字mới"})
        assert client.get("/api/lookup").json()["total"] == 10

    def test_a_row_with_no_picture_is_refused(self, client):
        login(client)
        assert client.post(
            "/api/data/row", data={"caption": "không ảnh"}
        ).status_code == 400

    def test_a_row_with_no_text_is_refused(self, client):
        login(client)
        assert client.post(
            "/api/data/row", data={"image_name": "orphan.jpg"}
        ).status_code == 400


class TestEditingGroundTruth:
    def test_a_correction_is_saved_and_searchable(self, client):
        login(client)
        body = client.post("/api/data/ground-truth", data={
            "index": 0, "ground_truth": "獨釣寒江雪",
            "expect_image": "sample_00.jpg"}).json()

        assert body["changed"] is True
        found = client.get("/api/lookup", params={"q": "獨釣寒江雪"}).json()
        assert found["total"] == 1 and found["items"][0]["index"] == 0
        # Replaced, not appended to.
        assert client.get("/api/lookup").json()["total"] == 10
        assert client.get("/api/lookup", params={"q": "落又一"}).json()["total"] == 0

    def test_the_previous_text_is_backed_up_first(self, client):
        login(client)
        client.post("/api/data/ground-truth", data={
            "index": 0, "ground_truth": "đã sửa"})
        backups = client.get("/api/data/backups").json()["items"]
        assert len(backups) == 1
        restored = client.get(f"/api/data/backups/{backups[0]['name']}")
        assert "花開花落又一秋" in restored.text

    def test_saving_the_same_text_writes_nothing(self, client):
        login(client)
        before = client.get("/api/lookup/0").json()["ground_truth"]
        body = client.post("/api/data/ground-truth", data={
            "index": 0, "ground_truth": before}).json()
        assert body["changed"] is False
        assert client.get("/api/data/backups").json()["items"] == []

    def test_a_stale_page_cannot_overwrite_the_wrong_row(self, client):
        """The guard that matters: a merge can move rows under an open page."""
        login(client)
        response = client.post("/api/data/ground-truth", data={
            "index": 0, "ground_truth": "sai chỗ",
            "expect_image": "sample_07.jpg"})
        assert response.status_code == 409
        assert "sample_07.jpg" in response.json()["detail"]
        assert client.get("/api/lookup", params={"q": "sai chỗ"}).json()["total"] == 0

    def test_a_row_number_past_the_end_is_refused(self, client):
        login(client)
        assert client.post("/api/data/ground-truth", data={
            "index": 999, "ground_truth": "x"}).status_code == 404

    def test_columns_the_app_does_not_model_survive_the_write(self, client):
        """Editing one row must not strip another row's extra columns."""
        login(client)
        client.post("/api/data/dataset/merge", files={"file": ("new.jsonl", jsonl_bytes([
            {"image": "extra_cols.jpg", "post_id": "UzpfSTE0NDk=",
             "post_link": URL, "ground_truth": "原文", "gemini_ocr": "機器"},
        ]))})
        client.post("/api/data/ground-truth", data={
            "index": 0, "ground_truth": "đã sửa"})

        found = client.get("/api/lookup", params={"q": "原文"}).json()["items"][0]
        assert found["extra"]["gemini_ocr"] == "機器"
        assert found["post_url"] == URL

    def test_only_an_admin_may_edit(self, client):
        login(client)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        assert client.post("/api/data/ground-truth", data={
            "index": 0, "ground_truth": "x"}).status_code == 403
