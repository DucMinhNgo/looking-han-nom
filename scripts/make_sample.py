"""Build the 10-item sample dataset that ships with the repo.

Deliberately imperfect: nine rows find their picture, one names a file nobody
copied across, and one file sits in the folder with no row describing it. That
is what a real handover looks like, and it means `docker compose up` shows the
mismatch handling working rather than a suspiciously clean screen.
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path("sample")
IMAGES = ROOT / "images"

ROWS = [
    ("花開花落又一秋\n時光匆匆似水流\n多少往事隨風去\n悠悠歲月染白頭",
     "Bốn câu thơ về thời gian trôi"),
    ("年歲漸長\n心要活得自由", "Thư pháp treo tường"),
    ("采菊東籬下\n悠然見南山", "Đào Uyên Minh"),
    ("會當凌絕頂\n一覽眾山小", "Đỗ Phủ — Vọng Nhạc"),
    ("海內存知己\n天涯若比鄰", "Vương Bột"),
    ("山重水複疑無路\n柳暗花明又一村", "Lục Du"),
    ("問渠那得清如許\n為有源頭活水來", "Chu Hy"),
    ("勸君更盡一杯酒\n西出陽關無故人", "Vương Duy"),
    ("落霞與孤鶩齊飛\n秋水共長天一色", "Đằng Vương các tự"),
    ("不畏浮雲遮望眼\n自緣身在最高層", "Vương An Thạch — ảnh chưa copy sang"),
]

PALETTE = [(250, 247, 238), (243, 238, 226), (236, 231, 216)]

# PIL's default font has no CJK glyphs, so every character would render as a
# tofu box and the shipped sample would look broken rather than illustrative.
CJK_FONTS = [
    "C:/Windows/Fonts/msjh.ttc",        # Microsoft JhengHei
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/msyh.ttc",        # Microsoft YaHei
    "C:/Windows/Fonts/batang.ttc",
    "C:/Windows/Fonts/mingliub.ttc",    # ExtB only — covers almost nothing common
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]

# A font file existing says nothing about whether it has the glyphs: MingLiU-ExtB
# is a real CJK font that covers only extension-B ideographs, so it renders 花 as
# an empty box. The only trustworthy check is to draw the character and look.
PROBE = "花開歲"


def _renders(font, char: str) -> bool:
    mask = font.getmask(char, mode="L")
    box = mask.getbbox()
    if box is None:
        return False
    # A tofu box is a hollow rectangle: its interior is blank. A real glyph has
    # ink away from its own edges.
    width, height = mask.size
    inset = [
        mask.getpixel((x, y))
        for y in range(height // 4, max(height // 4 + 1, 3 * height // 4))
        for x in range(width // 4, max(width // 4 + 1, 3 * width // 4))
    ]
    return any(inset)


def cjk_font(size: int):
    from PIL import ImageFont

    for path in CJK_FONTS:
        if not Path(path).exists():
            continue
        for index in range(4):  # .ttc files hold several faces
            try:
                font = ImageFont.truetype(path, size, index=index)
                if all(_renders(font, ch) for ch in PROBE):
                    print(f"  font: {Path(path).name} (face {index})")
                    return font
            except (OSError, ValueError):
                break
    print("  (no CJK font with the needed glyphs — they will render as boxes)")
    return None


def draw(path: Path, text: str, n: int, font=None) -> None:
    img = Image.new("RGB", (480, 640), PALETTE[n % 3])
    pen = ImageDraw.Draw(img)
    pen.rectangle([22, 22, 458, 618], outline=(120, 40, 40), width=3)

    # A single column, the way a hanging scroll reads.
    for i, ch in enumerate(text.replace("\n", "")[:9]):
        pen.text((214, 52 + i * 62), ch, fill=(30, 25, 20), font=font)
    pen.text((36, 598), f"sample {n + 1:02d}", fill=(150, 140, 130))
    img.save(path, quality=85)


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    font = cjk_font(52)
    lines = []

    for n, (ground_truth, caption) in enumerate(ROWS):
        name = f"sample_{n + 1:02d}.jpg"
        # Row 10 points at a file that is never written: a row without a picture.
        if n < len(ROWS) - 1:
            draw(IMAGES / name, ground_truth, n, font)
        lines.append({
            "image": name,
            "post_id": f"https://www.facebook.com/permalink.php"
                       f"?story_fbid=2783548982609{3100 + n}&id=100000593113258",
            "caption": caption,
            "ground_truth": ground_truth,
        })

    # And one picture nobody claims: an orphan.
    draw(IMAGES / "orphan_khong_co_dong.jpg", "無主", 7, font)

    (ROOT / "dataset.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines),
        encoding="utf-8",
    )
    print(f"rows {len(lines)} | images {len(list(IMAGES.glob('*.jpg')))}")


if __name__ == "__main__":
    main()
