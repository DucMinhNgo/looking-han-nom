import io
import json
import zipfile

import pytest

from app.core import ingest, intake
from app.core.dataset import Dataset

URL = "https://www.facebook.com/permalink.php?story_fbid=1&id=2"


def row(image="a.jpg", **overrides):
    base = {"image": image, "post_id": URL, "caption": "cap", "ground_truth": "文字"}
    base.update(overrides)
    return base


class TestParse:
    def test_reads_jsonl(self):
        rows, bad = ingest.parse_jsonl(
            json.dumps(row()).encode() + b"\n" + json.dumps(row("b.jpg")).encode()
        )
        assert len(rows) == 2 and bad == 0

    def test_a_byte_order_mark_does_not_corrupt_the_first_key(self):
        """Excel and PowerShell both export a BOM; without stripping it the
        first field name becomes '﻿image' and the row loses its picture."""
        raw = "﻿" + json.dumps(row(), ensure_ascii=False)
        rows, _ = ingest.parse_jsonl(raw.encode("utf-8"))
        assert rows[0]["image"] == "a.jpg"

    def test_broken_lines_are_counted_not_fatal(self):
        rows, bad = ingest.parse_jsonl(b'{"image":"a.jpg"}\n{ nope\n[1,2]\n')
        assert len(rows) == 1 and bad == 2

    def test_blank_lines_are_ignored(self):
        rows, bad = ingest.parse_jsonl(b'\n\n{"image":"a.jpg"}\n\n')
        assert len(rows) == 1 and bad == 0

    def test_cjk_survives(self):
        rows, _ = ingest.parse_jsonl(
            json.dumps(row(ground_truth="花開花落"), ensure_ascii=False).encode("utf-8")
        )
        assert rows[0]["ground_truth"] == "花開花落"


class TestMerge:
    def test_new_rows_are_appended(self):
        result = ingest.merge([row("a.jpg")], [row("b.jpg")])
        assert result.added == 1 and result.total == 2
        assert [r["image"] for r in result.rows] == ["a.jpg", "b.jpg"]

    def test_nothing_existing_is_lost(self):
        existing = [row(f"{i}.jpg") for i in range(50)]
        result = ingest.merge(existing, [row("new.jpg")])
        assert result.total == 51
        assert all(r in result.rows for r in existing)

    def test_the_same_picture_updates_in_place(self):
        result = ingest.merge(
            [row("a.jpg"), row("b.jpg")],
            [row("a.jpg", ground_truth="sửa rồi")],
        )
        assert result.updated == 1 and result.added == 0 and result.total == 2
        # Position is kept, so a merge does not reshuffle the page under anyone.
        assert result.rows[0]["ground_truth"] == "sửa rồi"
        assert result.rows[1]["image"] == "b.jpg"

    def test_an_identical_row_counts_as_unchanged(self):
        result = ingest.merge([row("a.jpg")], [row("a.jpg")])
        assert result.unchanged == 1 and result.changed is False

    def test_identity_ignores_path_and_case(self):
        """Matched the same tolerant way pictures are resolved."""
        result = ingest.merge(
            [row("images/A.JPG")], [row("a.jpg", ground_truth="mới")]
        )
        assert result.updated == 1 and result.total == 1

    def test_a_row_without_a_picture_is_always_appended(self):
        """There is nothing reliable to match it on, and guessing from caption
        text would silently merge two different entries."""
        result = ingest.merge([row("")], [row("")])
        assert result.added == 1 and result.total == 2
        assert result.appended_without_image == 1

    def test_a_batch_may_carry_both_new_and_updated(self):
        result = ingest.merge(
            [row("a.jpg"), row("b.jpg")],
            [row("a.jpg", caption="đổi"), row("c.jpg"), row("b.jpg")],
        )
        assert (result.added, result.updated, result.unchanged) == (1, 1, 1)
        assert result.total == 3

    def test_merging_into_an_empty_dataset(self):
        result = ingest.merge([], [row("a.jpg"), row("b.jpg")])
        assert result.added == 2 and result.total == 2

    def test_an_empty_batch_changes_nothing(self):
        result = ingest.merge([row("a.jpg")], [])
        assert result.changed is False and result.total == 1

    def test_merge_does_not_mutate_the_input(self):
        existing = [row("a.jpg")]
        ingest.merge(existing, [row("a.jpg", caption="đổi")])
        assert existing[0]["caption"] == "cap"


class TestWriteAndBackup:
    def test_written_rows_read_back(self, tmp_path):
        path = tmp_path / "d.jsonl"
        ingest.write_dataset(path, [row("a.jpg"), row("b.jpg", ground_truth="花開")])
        assert len(Dataset(path).items) == 2
        assert Dataset(path).items[1].ground_truth == "花開"

    def test_cjk_is_written_readable_not_escaped(self, tmp_path):
        path = tmp_path / "d.jsonl"
        ingest.write_dataset(path, [row(ground_truth="花開花落")])
        assert "花開花落" in path.read_text(encoding="utf-8")
        assert "\\u82b1" not in path.read_text(encoding="utf-8")

    def test_a_backup_is_taken_before_overwriting(self, tmp_path):
        path = tmp_path / "d.jsonl"
        ingest.write_dataset(path, [row("a.jpg")])
        saved = ingest.backup(path, tmp_path / "backups")

        ingest.write_dataset(path, [row("b.jpg")])
        assert len(Dataset(path).items) == 1
        # The previous corpus is still recoverable.
        assert Dataset(saved).items[0].image == "a.jpg"

    def test_backing_up_a_missing_file_is_not_an_error(self, tmp_path):
        assert ingest.backup(tmp_path / "nope.jsonl", tmp_path / "b") is None

    def test_old_backups_are_pruned(self, tmp_path):
        path = tmp_path / "d.jsonl"
        ingest.write_dataset(path, [row("a.jpg")])
        for _ in range(8):
            ingest.backup(path, tmp_path / "backups", keep=3)
        assert len(list((tmp_path / "backups").glob("*.jsonl"))) == 3

    def test_no_stray_part_file_is_left_behind(self, tmp_path):
        path = tmp_path / "d.jsonl"
        ingest.write_dataset(path, [row("a.jpg")])
        assert list(tmp_path.glob("*.part")) == []


class TestImageIntake:
    def test_saves_uploaded_pictures(self, tmp_path):
        result = intake.save_images(
            [("a.jpg", b"\xff\xd8one"), ("b.png", b"\x89PNGtwo")], tmp_path / "images"
        )
        assert result.saved == ["a.jpg", "b.png"]
        assert (tmp_path / "images" / "a.jpg").read_bytes() == b"\xff\xd8one"

    def test_non_images_are_skipped(self, tmp_path):
        result = intake.save_images(
            [("notes.txt", b"hi"), ("a.jpg", b"\xff\xd8")], tmp_path / "images"
        )
        assert result.saved == ["a.jpg"] and result.skipped_not_image == ["notes.txt"]

    def test_an_existing_name_is_a_conflict_not_an_overwrite(self, tmp_path):
        """Replacing one picture with another would change what a row means
        without anybody noticing."""
        images = tmp_path / "images"
        intake.save_images([("a.jpg", b"original")], images)
        result = intake.save_images([("a.jpg", b"different")], images)

        assert result.saved == [] and result.skipped_conflict == ["a.jpg"]
        assert (images / "a.jpg").read_bytes() == b"original"

    def test_overwrite_is_possible_when_asked_for(self, tmp_path):
        images = tmp_path / "images"
        intake.save_images([("a.jpg", b"original")], images)
        intake.save_images([("a.jpg", b"newer")], images, overwrite=True)
        assert (images / "a.jpg").read_bytes() == b"newer"

    @pytest.mark.parametrize(
        "name", ["../escape.jpg", "../../etc/passwd.jpg", "/abs/path.jpg"]
    )
    def test_a_crafted_name_cannot_escape_the_folder(self, tmp_path, name):
        images = tmp_path / "images"
        intake.save_images([(name, b"\xff\xd8")], images)
        assert not (tmp_path / "escape.jpg").exists()
        # It lands flattened inside the folder, or not at all.
        for path in tmp_path.rglob("*.jpg"):
            assert images.resolve() in path.resolve().parents

    def test_an_oversized_picture_is_refused(self, tmp_path):
        with pytest.raises(intake.IntakeError, match="over the"):
            intake.save_images(
                [("a.jpg", b"x" * 2048)], tmp_path / "images", max_bytes=1024
            )


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


JPEG = bytes.fromhex("ffd8")


class TestZipIntake:
    def test_unpacks_the_pictures(self, tmp_path):
        data = make_zip([("a.jpg", b"\xff\xd8one"), ("b.png", b"\x89PNGtwo")])
        result = intake.extract_zip(data, tmp_path / "images")
        assert sorted(result.saved) == ["a.jpg", "b.png"]

    def test_folders_inside_the_zip_are_kept(self, tmp_path):
        """A folder per post is how the pictures are filed; it must survive."""
        data = make_zip([("batch/2026/a.jpg", JPEG)])
        result = intake.extract_zip(data, tmp_path / "images")
        assert result.saved == ["batch/2026/a.jpg"]
        assert (tmp_path / "images" / "batch" / "2026" / "a.jpg").is_file()

    def test_one_picture_per_post_folder_does_not_collide(self, tmp_path):
        """Flattening this would leave one file and nine "conflicts"."""
        data = make_zip([(f"{100 + n}/1.jpg", JPEG + bytes([n])) for n in range(10)])
        result = intake.extract_zip(data, tmp_path / "images")

        assert len(result.saved) == 10 and result.skipped_conflict == []
        assert sorted(result.saved)[0] == "100/1.jpg"
        # Ten distinct files, each still carrying its own bytes.
        assert (tmp_path / "images" / "100" / "1.jpg").read_bytes()[-1] == 0
        assert (tmp_path / "images" / "109" / "1.jpg").read_bytes()[-1] == 9

    def test_zip_slip_is_refused(self, tmp_path):
        """A zip entry may name ../../ and a naive extract would obey it."""
        data = make_zip([("../../escaped.jpg", b"\xff\xd8")])
        intake.extract_zip(data, tmp_path / "images")
        assert not (tmp_path.parent / "escaped.jpg").exists()
        assert not (tmp_path / "escaped.jpg").exists()

    def test_non_images_in_the_zip_are_ignored(self, tmp_path):
        data = make_zip([("a.jpg", b"\xff\xd8"), ("readme.txt", b"hi")])
        result = intake.extract_zip(data, tmp_path / "images")
        assert result.saved == ["a.jpg"] and "readme.txt" in result.skipped_not_image

    def test_mac_metadata_is_ignored(self, tmp_path):
        """A Mac zip carries a shadow tree whose entries look like the real ones."""
        data = make_zip([("a.jpg", b"\xff\xd8"), ("__MACOSX/._a.jpg", b"junk")])
        result = intake.extract_zip(data, tmp_path / "images")
        assert result.saved == ["a.jpg"]

    def test_existing_pictures_are_conflicts(self, tmp_path):
        images = tmp_path / "images"
        intake.save_images([("a.jpg", b"original")], images)
        result = intake.extract_zip(make_zip([("a.jpg", b"newer")]), images)
        assert result.skipped_conflict == ["a.jpg"]
        assert (images / "a.jpg").read_bytes() == b"original"

    def test_a_file_that_is_not_a_zip_says_so(self, tmp_path):
        with pytest.raises(intake.IntakeError, match="not a readable zip"):
            intake.extract_zip(b"this is not a zip", tmp_path / "images")

    def test_a_zip_that_unpacks_too_large_is_refused(self, tmp_path):
        data = make_zip([("a.jpg", b"x" * 5000)])
        with pytest.raises(intake.IntakeError, match="over the"):
            intake.extract_zip(data, tmp_path / "images", max_unpacked=1024)
