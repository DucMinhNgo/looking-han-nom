# -*- coding: utf-8 -*-
"""
Gộp font chỉ bằng fontTools (không cần FontForge).
Xử lý các lỗi phổ biến: unitsPerEm khác nhau, bad glyph flags, too much data.
"""

from pathlib import Path
import shutil
from copy import deepcopy

from fontTools.ttLib import TTFont, newTable
from fontTools.merge import Merger
from fontTools.ttLib.tables._g_l_y_f import Glyph


def normalize_font(font: TTFont, target_upem: int = 1024) -> TTFont:
    """Chuẩn hóa unitsPerEm và scale glyph nếu cần."""
    try:
        head = font["head"]
        current_upem = head.unitsPerEm
        if current_upem == target_upem:
            return font

        scale = target_upem / current_upem
        head.unitsPerEm = target_upem

        # Scale glyf
        if "glyf" in font:
            glyf = font["glyf"]
            for name in glyf.keys():
                g = glyf[name]
                if g.numberOfContours == 0:
                    continue
                if hasattr(g, "coordinates") and g.coordinates:
                    g.coordinates = g.coordinates.__class__(
                        [(int(x * scale), int(y * scale)) for x, y in g.coordinates]
                    )
                if hasattr(g, "xMin"):
                    g.xMin = int(g.xMin * scale)
                    g.yMin = int(g.yMin * scale)
                    g.xMax = int(g.xMax * scale)
                    g.yMax = int(g.yMax * scale)

        # Scale hmtx
        if "hmtx" in font:
            hmtx = font["hmtx"].metrics
            for name in list(hmtx.keys()):
                w, lsb = hmtx[name]
                hmtx[name] = (int(w * scale), int(lsb * scale))

        # Scale OS/2
        if "OS/2" in font:
            os2 = font["OS/2"]
            for attr in ["sTypoAscender", "sTypoDescender", "sTypoLineGap",
                         "usWinAscent", "usWinDescent", "sxHeight", "sCapHeight"]:
                if hasattr(os2, attr):
                    setattr(os2, attr, int(getattr(os2, attr) * scale))

        return font
    except Exception as e:
        print(f"    ⚠ normalize lỗi: {e}")
        return font


def safe_merge(ttf_files: list[Path], output_file: Path) -> bool:
    """Gộp an toàn hơn."""
    try:
        # Đọc + chuẩn hóa tất cả font về cùng unitsPerEm = 1024
        fonts = []
        for f in ttf_files:
            font = TTFont(str(f), recalcBBoxes=False, recalcTimestamp=False)
            font = normalize_font(font, target_upem=1024)
            # Lưu tạm để Merger đọc lại
            tmp = output_file.parent / f"_tmp_{f.stem}.ttf"
            font.save(str(tmp))
            fonts.append(tmp)
            font.close()

        merger = Merger()
        merged = merger.merge([str(f) for f in fonts])
        merged.save(str(output_file))

        # Xóa file tạm
        for tmp in fonts:
            tmp.unlink(missing_ok=True)

        return True
    except Exception as e:
        print(f"  ⚠ safe_merge lỗi: {e}")
        # Dọn file tạm nếu còn
        for tmp in output_file.parent.glob("_tmp_*.ttf"):
            tmp.unlink(missing_ok=True)
        return False


def merge_fonts_in_folder(folder: Path, output_file: Path) -> bool:
    ttf_files = sorted([
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in {".ttf", ".tff"}
    ])

    if not ttf_files:
        print(f"  ⚠ Không có file .ttf")
        return False

    if len(ttf_files) == 1:
        shutil.copy2(ttf_files[0], output_file)
        print(f"  ✅ Chỉ 1 file → copy: {ttf_files[0].name}")
        return True

    print(f"  🔄 Đang gộp {len(ttf_files)} file:")
    for f in ttf_files:
        print(f"      - {f.name}")

    # Thử gộp an toàn
    if safe_merge(ttf_files, output_file):
        print(f"  ✅ Đã tạo: {output_file.name}")
        return True

    # Fallback cuối: lấy file lớn nhất
    largest = max(ttf_files, key=lambda x: x.stat().st_size)
    shutil.copy2(largest, output_file)
    print(f"  ⚠ Không gộp được → lấy file lớn nhất: {largest.name}")
    return True


def process_all_folders(source_dir: str = "font", output_dir: str = "tff"):
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        print(f"❌ Không tìm thấy: {source}")
        return

    subfolders = [d for d in source.iterdir() if d.is_dir()]
    print(f"📂 Tìm thấy {len(subfolders)} thư mục con\n")

    success = 0
    for folder in sorted(subfolders):
        out_name = folder.name.lower() + ".ttf"
        out_path = output / out_name
        print(f"▶ Xử lý: {folder.name}/  →  {out_name}")
        if merge_fonts_in_folder(folder, out_path):
            success += 1
        print()

    print(f"🎉 Hoàn thành! Đã tạo {success}/{len(subfolders)} file trong: {output}")


if __name__ == "__main__":
    SOURCE_FOLDER = "font"
    OUTPUT_FOLDER = "app/static/fonts"
    process_all_folders(SOURCE_FOLDER, OUTPUT_FOLDER)