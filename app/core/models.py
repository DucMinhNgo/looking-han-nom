"""The one shape this tool moves around.

A dataset row and the image file it points at, if that file turned out to
exist. Whether it exists is not an afterthought — a row whose picture is
missing is still worth reading, and a picture with no row is still worth
knowing about, so both states are first-class rather than filtered away.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

# Facebook permalinks carry the post's identity as digits inside the URL, so a
# search for the bare number has to reach them.
_DIGITS = re.compile(r"\d{6,}")


def normalize(text: str) -> str:
    """NFC-fold and casefold, for comparing and for searching.

    NFC matters here as much as anywhere in this project: the same CJK
    character can arrive composed or decomposed, and a search that does not
    fold them finds nothing while looking like it worked.
    """
    return unicodedata.normalize("NFC", str(text or "")).casefold()


@dataclass
class Item:
    """One row of the dataset, joined to its image if one was found."""

    index: int
    image: str
    post_id: str
    caption: str = ""
    ground_truth: str = ""
    # Filled in by the image library, not by the loader.
    image_name: str = ""
    has_image: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def post_url(self) -> str:
        """``post_id`` is already a Facebook URL in this dataset.

        Anything that is not a URL is still shown, but is not offered as a
        link — a button that goes nowhere is worse than no button.
        """
        value = (self.post_id or "").strip()
        return value if value.startswith(("http://", "https://")) else ""

    @property
    def post_number(self) -> str:
        """The longest run of digits in the post id, for display and search."""
        matches = _DIGITS.findall(self.post_id or "")
        return max(matches, key=len) if matches else ""

    def haystack(self, field_name: str = "") -> str:
        """The text a query is matched against, normalized once per search."""
        if field_name == "post":
            parts = [self.post_id, self.post_number]
        elif field_name == "caption":
            parts = [self.caption]
        elif field_name == "ground_truth":
            parts = [self.ground_truth]
        elif field_name == "image":
            parts = [self.image, self.image_name]
        else:
            parts = [
                self.post_id, self.post_number, self.caption,
                self.ground_truth, self.image, self.image_name,
            ]
        # Joined with a separator no query will contain, so a search cannot
        # match across the seam between two fields.
        return normalize(" | ".join(p for p in parts if p))

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "image": self.image,
            "image_name": self.image_name,
            "has_image": self.has_image,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "post_number": self.post_number,
            "caption": self.caption,
            "ground_truth": self.ground_truth,
            "extra": self.extra,
        }
