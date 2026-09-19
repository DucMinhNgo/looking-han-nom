import json

import pytest

from app.core.dataset import Dataset
from app.core.models import Item

POEM = "花開花落又一秋\n時光匆匆似水流"
URL = "https://www.facebook.com/permalink.php?story_fbid=27835489826093100&id=100000593113258"


def write(path, rows):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def row(**overrides):
    base = {
        "image": "sample_01.jpg",
        "post_id": URL,
        "caption": "Bốn câu thơ về thời gian trôi",
        "ground_truth": POEM,
    }
    base.update(overrides)
    return base


@pytest.fixture
def dataset(tmp_path):
    write(tmp_path / "d.jsonl", [
        row(),
        row(image="sample_02.jpg", ground_truth="采菊東籬下", caption="Đào Uyên Minh"),
        row(image="sample_03.jpg", ground_truth="海內存知己", caption="Vương Bột",
            post_id=URL.replace("27835489826093100", "99988877766655544")),
    ])
    return Dataset(tmp_path / "d.jsonl")


class TestLoading:
    def test_reads_the_four_fields(self, dataset):
        item = dataset.items[0]
        assert item.image == "sample_01.jpg"
        assert item.post_id == URL
        assert item.caption.startswith("Bốn câu")
        assert item.ground_truth == POEM

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path):
        empty = Dataset(tmp_path / "nope.jsonl")
        assert empty.items == [] and empty.exists is False

    def test_a_torn_line_does_not_cost_the_file(self, tmp_path):
        path = tmp_path / "d.jsonl"
        path.write_text(
            json.dumps(row(), ensure_ascii=False) + "\n{ broken\n",
            encoding="utf-8",
        )
        data = Dataset(path)
        assert len(data.items) == 1
        assert data.malformed_lines == 1

    def test_header_aliases_are_tolerated(self, tmp_path):
        """The file is produced elsewhere and its spellings have drifted before."""
        path = write(tmp_path / "d.jsonl", [{
            "img": "a.jpg", "post_url": URL,
            "fb_caption": "cap", "gt": "文字",
        }])
        item = Dataset(path).items[0]
        assert item.image == "a.jpg" and item.post_id == URL
        assert item.caption == "cap" and item.ground_truth == "文字"

    def test_unknown_columns_are_kept_not_dropped(self, tmp_path):
        path = write(tmp_path / "d.jsonl", [row(reviewer="mai", score=0.9)])
        assert Dataset(path).items[0].extra == {"reviewer": "mai", "score": 0.9}

    def test_an_edited_file_is_picked_up_without_a_restart(self, tmp_path, dataset):
        assert len(dataset.items) == 3
        write(tmp_path / "d.jsonl", [row(), row(image="b.jpg")])
        assert len(dataset.items) == 2


class TestPostUrl:
    def test_a_facebook_url_becomes_a_link(self, dataset):
        assert dataset.items[0].post_url == URL

    def test_a_non_url_offers_no_link(self):
        """A button that goes nowhere is worse than no button."""
        assert Item(index=0, image="a.jpg", post_id="12345").post_url == ""

    def test_the_numeric_id_is_pulled_out_of_the_url(self, dataset):
        assert dataset.items[0].post_number == "27835489826093100"


class TestSearch:
    def test_finds_cjk_by_substring(self, dataset):
        """No whitespace to tokenize on — substring is the only thing that works."""
        assert dataset.search("落又一").total == 1

    def test_finds_vietnamese_caption(self, dataset):
        assert dataset.search("Đào Uyên").total == 1

    def test_is_case_insensitive(self, dataset):
        assert dataset.search("ĐÀO UYÊN").total == 1

    def test_finds_a_bare_post_number_inside_the_url(self, dataset):
        """Someone pasting just the id should not have to paste the whole link."""
        assert dataset.search("99988877766655544").total == 1

    def test_finds_a_whole_facebook_link(self, dataset):
        """Two of the three rows are images of the same post, so both match."""
        assert dataset.search(URL).total == 2

    def test_an_empty_query_returns_everything(self, dataset):
        assert dataset.search("").total == 3

    def test_no_match_is_empty_not_an_error(self, dataset):
        assert dataset.search("không có gì").total == 0

    def test_a_field_filter_narrows_the_search(self, dataset):
        # The poem is in ground_truth, never in the caption.
        assert dataset.search("采菊", field="ground_truth").total == 1
        assert dataset.search("采菊", field="caption").total == 0

    def test_search_does_not_match_across_two_fields(self, dataset):
        """The haystack seam must not create matches that are not there."""
        assert dataset.search("trôi花開").total == 0

    def test_paging(self, dataset):
        first = dataset.search("", offset=0, limit=2)
        assert len(first.items) == 2 and first.to_json()["has_more"] is True
        second = dataset.search("", offset=2, limit=2)
        assert len(second.items) == 1 and second.to_json()["has_more"] is False


class TestStats:
    def test_counts_what_is_present(self, dataset):
        stats = dataset.stats()
        assert stats["rows"] == 3
        assert stats["with_ground_truth"] == 3
        assert stats["with_post_url"] == 3
        assert stats["posts"] == 2


class TestAddingRowsLater:
    def test_appended_rows_appear(self, tmp_path, dataset):
        assert len(dataset.items) == 3
        with open(tmp_path / "d.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row(image="sample_04.jpg"), ensure_ascii=False) + "\n")
        assert len(dataset.items) == 4

    def test_an_atomic_replace_is_seen(self, tmp_path, dataset):
        """How editors save: write a temp file, rename it over the original."""
        assert len(dataset.items) == 3
        tmp = tmp_path / "next.jsonl"
        tmp.write_text(json.dumps(row(), ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(tmp_path / "d.jsonl")
        assert len(dataset.items) == 1

    def test_a_dataset_that_appears_later_is_picked_up(self, tmp_path):
        """Starting the app before the file exists must recover on its own."""
        path = tmp_path / "later.jsonl"
        data = Dataset(path)
        assert data.items == [] and data.exists is False

        write(path, [row()])
        assert len(data.items) == 1 and data.exists is True

    def test_new_rows_are_searchable_immediately(self, tmp_path, dataset):
        assert dataset.search("獨釣寒江雪").total == 0
        with open(tmp_path / "d.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                row(image="s.jpg", ground_truth="獨釣寒江雪"), ensure_ascii=False) + "\n")
        assert dataset.search("獨釣寒江雪").total == 1


class TestAPostIdThatIsNotALink:
    """Real exports carry an opaque base64 post_id and the URL beside it.

    Taking the first non-empty alias hands back the blob and leaves the row
    with no link at all, which is how a freshly merged batch ended up with
    no way to open the post it came from.
    """

    ROW = {
        "image": "a.jpg",
        "post_id": "UzpfSTEwMDAwMjI1NDQ4NjA3NjpWSzoyNTYwNzYyNjY5MjIxMjc2OQ==",
        "post_link": "https://www.facebook.com/permalink.php?story_fbid=25607626692212769&id=100002254486076",
        "ground_truth": "聲家振棣棠風春",
        "gemini_ocr": "瑞氣盈門迎幸福",
    }

    def _one(self, tmp_path, row):
        path = tmp_path / "d.jsonl"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        data = Dataset(path)
        data.load(force=True)
        return data.items[0]

    def test_the_link_is_found_in_its_own_column(self, tmp_path):
        item = self._one(tmp_path, self.ROW)
        assert item.post_url == self.ROW["post_link"]
        assert item.post_id == self.ROW["post_id"]

    def test_the_real_story_id_is_read_from_the_url(self, tmp_path):
        """The base64 blob has digits in it that mean nothing."""
        item = self._one(tmp_path, self.ROW)
        assert item.post_number == "25607626692212769"

    def test_an_unused_alias_survives_a_rewrite(self, tmp_path):
        """post_link must reach ``extra``, or the next write deletes it."""
        item = self._one(tmp_path, self.ROW)
        assert item.extra["post_link"] == self.ROW["post_link"]
        assert item.extra["gemini_ocr"] == self.ROW["gemini_ocr"]

    def test_the_link_is_searchable(self, tmp_path):
        item = self._one(tmp_path, self.ROW)
        assert "25607626692212769" in item.haystack()
        assert "25607626692212769" in item.haystack("post")

    def test_a_plain_url_in_post_id_still_works(self, tmp_path):
        """The shipped sample spells it the other way round."""
        item = self._one(tmp_path, {"image": "a.jpg", "post_id": "https://fb.com/x/9"})
        assert item.post_url == "https://fb.com/x/9"
        assert item.extra == {}

    def test_a_row_with_no_url_anywhere_is_offered_no_link(self, tmp_path):
        item = self._one(tmp_path, {"image": "a.jpg", "post_id": "UzpfSTE0NDk="})
        assert item.post_url == ""
