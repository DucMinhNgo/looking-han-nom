import pytest

from app.core.localimages import ImageLibrary, basename, is_image
from app.core.models import Item

JPEG = bytes.fromhex("ffd8")


def make_images(root, names):
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xd8fake-jpeg")
    return root


def items(*names):
    return [Item(index=i, image=n, post_id="") for i, n in enumerate(names)]


@pytest.fixture
def library(tmp_path):
    make_images(tmp_path / "images", ["a.jpg", "B.JPG", "nested/c.png", "d.jpeg"])
    return ImageLibrary(tmp_path / "images")


class TestHelpers:
    def test_is_image_by_extension(self):
        assert is_image("x.jpg") and is_image("x.PNG")
        assert not is_image("notes.txt") and not is_image("noext")

    def test_basename_strips_either_separator(self):
        assert basename("images/x.jpg") == "x.jpg"
        assert basename("images\\x.jpg") == "x.jpg"


class TestResolve:
    def test_exact_name(self, library):
        assert library.resolve("a.jpg") == "a.jpg"

    def test_a_path_prefix_is_stripped(self, library):
        """The dataset often carries `images/x.jpg` while the folder is flat."""
        assert library.resolve("images/a.jpg") == "a.jpg"
        assert library.resolve("some\\deep\\a.jpg") == "a.jpg"

    def test_case_insensitively(self, library):
        assert library.resolve("b.jpg") == "B.JPG"

    def test_ignoring_the_extension(self, library):
        """`.jpeg` that became `.jpg` is the commonest drift of all."""
        assert library.resolve("d.jpg") == "d.jpeg"

    def test_a_nested_file_is_found_by_its_basename(self, library):
        assert library.resolve("c.png") == "nested/c.png"

    def test_an_unknown_name_resolves_to_nothing(self, library):
        assert library.resolve("ghost.jpg") is None

    def test_an_empty_name_resolves_to_nothing(self, library):
        assert library.resolve("") is None and library.resolve(None) is None


class TestPathContainment:
    def test_a_real_file_resolves(self, library):
        assert library.path_for("a.jpg").name == "a.jpg"

    @pytest.mark.parametrize(
        "name", ["../secret.txt", "../../etc/passwd", "nested/../../out.jpg"]
    )
    def test_traversal_is_refused(self, library, tmp_path, name):
        """The alphabet is not the guard — the containment check is."""
        (tmp_path / "secret.txt").write_bytes(b"secret")
        assert library.path_for(name) is None

    def test_an_absolute_path_is_refused(self, library, tmp_path):
        assert library.path_for(str(tmp_path / "secret.txt")) is None


class TestMismatch:
    def test_everything_lines_up(self, library):
        report = library.attach(items("a.jpg", "B.JPG", "c.png", "d.jpeg"))
        assert report.matched == 4
        assert report.rows_without_image == 0
        assert report.orphans == 0

    def test_a_row_without_its_picture_is_counted_not_dropped(self, library):
        rows = items("a.jpg", "ghost.jpg")
        report = library.attach(rows)
        assert report.matched == 1 and report.rows_without_image == 1
        # The row survives and is still readable — only its picture is missing.
        assert rows[1].has_image is False and rows[1].image == "ghost.jpg"

    def test_a_picture_without_a_row_is_reported_as_an_orphan(self, library):
        report = library.attach(items("a.jpg"))
        assert report.orphans == 3
        assert "d.jpeg" in report.orphan_images

    def test_both_kinds_of_drift_at_once(self, library):
        report = library.attach(items("a.jpg", "ghost.jpg"))
        assert (report.matched, report.rows_without_image, report.orphans) == (1, 1, 3)
        assert report.scanned == 4

    def test_two_rows_may_share_one_picture(self, library):
        """Not an orphan and not a miss — just the same file used twice."""
        report = library.attach(items("a.jpg", "a.jpg"))
        assert report.matched == 2 and report.rows_without_image == 0
        assert "a.jpg" not in report.orphan_images

    def test_attach_marks_the_items_in_place(self, library):
        rows = items("images/a.jpg")
        library.attach(rows)
        assert rows[0].has_image and rows[0].image_name == "a.jpg"

    def test_a_missing_folder_is_reported_not_crashed(self, tmp_path):
        library = ImageLibrary(tmp_path / "nope")
        report = library.attach(items("a.jpg"))
        assert report.exists is False
        assert report.scanned == 0 and report.rows_without_image == 1

    def test_non_images_in_the_folder_are_ignored(self, tmp_path):
        root = make_images(tmp_path / "images", ["a.jpg"])
        (root / "notes.txt").write_text("hello", encoding="utf-8")
        report = ImageLibrary(root).attach(items("a.jpg"))
        assert report.scanned == 1 and report.orphans == 0

    def test_a_new_file_is_seen_without_a_restart(self, library, tmp_path):
        assert library.resolve("late.jpg") is None
        (tmp_path / "images" / "late.jpg").write_bytes(b"\xff\xd8x")
        assert library.scan(force=True) and library.resolve("late.jpg") == "late.jpg"

    def test_the_orphan_sample_is_capped(self, tmp_path):
        root = make_images(tmp_path / "images", [f"x{i:04d}.jpg" for i in range(250)])
        report = ImageLibrary(root).attach([])
        assert report.orphans == 250
        assert len(report.to_json()["orphan_sample"]) == 200


class TestAddingDataLater:
    """What happens when somebody drops in more pictures or more rows.

    The answer has to be "it appears", because the alternative is a user who
    copied a folder across and sees nothing, with no way to tell whether the
    app is broken or their paths are wrong.
    """

    def test_a_new_file_at_the_top_level_appears_at_once(self, library, tmp_path):
        assert library.resolve("late.jpg") is None
        (tmp_path / "images" / "late.jpg").write_bytes(b"\xff\xd8x")
        # The directory's own mtime moved, so no waiting and no button.
        assert library.resolve("late.jpg") == "late.jpg"

    def test_a_deleted_file_disappears_at_once(self, library, tmp_path):
        assert library.resolve("a.jpg") == "a.jpg"
        (tmp_path / "images" / "a.jpg").unlink()
        assert library.resolve("a.jpg") is None

    def test_a_new_file_in_a_subfolder_appears_once_the_scan_goes_stale(
        self, tmp_path
    ):
        """A parent's mtime does not move when a child changes, so this one is
        bounded by the TTL rather than seen immediately."""
        root = make_images(tmp_path / "images", ["a.jpg", "sub/b.jpg"])
        library = ImageLibrary(root, ttl_s=1000)  # effectively "never expire"
        assert len(library.files) == 2

        (root / "sub" / "c.jpg").write_bytes(b"\xff\xd8x")
        assert len(library.files) == 2, "not expected to be seen yet"

        library.ttl_s = 0  # as if the TTL had elapsed
        assert len(library.files) == 3

    def test_the_rescan_button_never_has_to_wait(self, tmp_path):
        root = make_images(tmp_path / "images", ["sub/a.jpg"])
        library = ImageLibrary(root, ttl_s=10_000)
        assert len(library.files) == 1
        (root / "sub" / "b.jpg").write_bytes(b"\xff\xd8x")
        assert len(library.scan(force=True)) == 2

    def test_a_folder_that_appears_later_is_picked_up(self, tmp_path):
        """Starting the app before the pictures are copied across must recover."""
        images = tmp_path / "images"
        library = ImageLibrary(images, ttl_s=0)
        assert library.attach(items("a.jpg")).exists is False

        make_images(images, ["a.jpg"])
        report = library.attach(items("a.jpg"))
        assert report.exists is True and report.matched == 1

    def test_new_pictures_turn_missing_rows_into_matches(self, tmp_path):
        root = make_images(tmp_path / "images", ["a.jpg"])
        library = ImageLibrary(root, ttl_s=0)
        rows = items("a.jpg", "b.jpg")

        assert library.attach(rows).rows_without_image == 1
        (root / "b.jpg").write_bytes(b"\xff\xd8x")
        report = library.attach(rows)
        assert report.rows_without_image == 0 and report.matched == 2


class TestOneFolderPerPost:
    """Pictures filed as <post id>/1.jpg, which is how the crawl stores them.

    The basenames stop being unique the moment there is more than one post, so
    everything here is about not attaching one post's picture to another
    post's text.
    """

    POSTS = ["10003340783103883", "10006428582795103", "10007271009377527"]

    @pytest.fixture
    def library(self, tmp_path):
        images = tmp_path / "images"
        for n, post in enumerate(self.POSTS):
            (images / post).mkdir(parents=True)
            (images / post / "1.jpg").write_bytes(JPEG + bytes([n]))
        # One post with several pictures, numbered from something other than 1.
        (images / "10014375832000378").mkdir(parents=True)
        (images / "10014375832000378" / "4.jpg").write_bytes(JPEG)
        (images / "10014375832000378" / "5.jpg").write_bytes(JPEG)
        return ImageLibrary(images)

    def test_every_row_finds_its_own_folder(self, library):
        """The whole point: nine rows saying 1.jpg are nine different files."""
        for post in self.POSTS:
            assert library.resolve(f"/images/{post}/1.jpg") == f"{post}/1.jpg"

    def test_a_stripped_prefix_of_any_depth_still_matches(self, library):
        post = self.POSTS[0]
        for spelled in (
            f"/images/{post}/1.jpg",
            f"images/{post}/1.jpg",
            f"{post}/1.jpg",
            f"./data/exports/images/{post}/1.jpg",
        ):
            assert library.resolve(spelled) == f"{post}/1.jpg", spelled

    def test_a_bare_shared_basename_is_refused_not_guessed(self, library):
        """Three folders have a 1.jpg. Returning one of them would be wrong."""
        assert library.resolve("1.jpg") is None
        assert library.is_ambiguous("1.jpg") is True

    def test_a_bare_unique_basename_still_matches(self, library):
        """Only one folder has a 5.jpg, so the old tolerance still applies."""
        assert library.resolve("5.jpg") == "10014375832000378/5.jpg"
        assert library.is_ambiguous("5.jpg") is False

    def test_backslashes_and_case_survive(self, library):
        post = self.POSTS[1]
        back = chr(92)  # a Windows-style path, written without escapes
        assert library.resolve(f"images{back}{post}{back}1.jpg") == f"{post}/1.jpg"
        assert library.resolve(f"/IMAGES/{post}/1.JPG") == f"{post}/1.jpg"

    def test_the_right_bytes_are_served(self, library):
        """Resolving to the right name is only half of it."""
        for n, post in enumerate(self.POSTS):
            path = library.path_for(library.resolve(f"/images/{post}/1.jpg"))
            assert path.read_bytes()[-1] == n

    def test_the_rest_of_a_post_folder_comes_with_the_row(self, library):
        """A post holds 4.jpg and 5.jpg; only 4.jpg has a row of its own."""
        rows = [Item(index=i, image=f"/images/{p}/1.jpg", post_id="")
                for i, p in enumerate(self.POSTS)]
        rows.append(Item(index=3, image="/images/10014375832000378/4.jpg", post_id=""))
        report = library.attach(rows)

        assert report.matched == 4 and report.rows_without_image == 0
        assert rows[3].siblings == ["10014375832000378/5.jpg"]
        # Reachable through its post, so not an orphan — but counted, because
        # it still has no ground truth.
        assert report.orphan_images == [] and report.sibling_images == 1

    def test_a_post_with_one_picture_has_no_siblings(self, library):
        rows = [Item(index=0, image=f"/images/{self.POSTS[0]}/1.jpg", post_id="")]
        library.attach(rows)
        assert rows[0].siblings == []

    def test_a_row_another_row_describes_is_not_a_companion(self, library):
        """Both pictures of the post have their own text; neither is spare."""
        rows = [
            Item(index=0, image="/images/10014375832000378/4.jpg", post_id=""),
            Item(index=1, image="/images/10014375832000378/5.jpg", post_id=""),
        ]
        report = library.attach(rows)
        assert report.matched == 2 and report.sibling_images == 0
        # Each still lists the other, so either row can show the whole post.
        assert rows[0].siblings == ["10014375832000378/5.jpg"]
        assert rows[1].siblings == ["10014375832000378/4.jpg"]

    def test_a_picture_in_a_folder_no_row_mentions_is_still_an_orphan(self, library):
        (library.images_dir / "900999").mkdir()
        (library.images_dir / "900999" / "1.jpg").write_bytes(JPEG)
        rows = [Item(index=0, image=f"/images/{self.POSTS[0]}/1.jpg", post_id="")]
        report = library.attach(rows)
        assert "900999/1.jpg" in report.orphan_images

    def test_a_picture_at_the_top_level_has_no_siblings(self, library):
        """At the root the folder is the whole library, not a post."""
        (library.images_dir / "loose.jpg").write_bytes(JPEG)
        library.scan(force=True)
        assert library.siblings("loose.jpg") == []

    def test_a_row_naming_a_folder_that_was_never_copied(self, library):
        rows = [Item(index=0, image="/images/99999999999/1.jpg", post_id="")]
        report = library.attach(rows)
        assert report.rows_without_image == 1 and rows[0].has_image is False
        # Not attached to some other post's 1.jpg, which is the failure that
        # would have looked like success.
        assert rows[0].image_name == ""

    def test_a_path_cannot_escape_the_folder(self, library):
        assert library.path_for("../../../etc/passwd") is None
        assert library.path_for("10003340783103883/../../secret.jpg") is None
