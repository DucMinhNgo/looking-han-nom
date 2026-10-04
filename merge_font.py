from pathlib import Path
from fontTools.ttLib import TTFont

def convert_ttf_to_woff2(font_dir="app/static/fonts"):
    font_path = Path(font_dir).resolve()
    ttf_files = list(font_path.glob("*.ttf"))

    print(f"📂 Tìm thấy {len(ttf_files)} file TTF trong {font_path}\n")

    for ttf_file in ttf_files:
        woff2_file = ttf_file.with_suffix(".woff2")
        print(f"▶ Đang nén: {ttf_file.name} ({ttf_file.stat().st_size // 1024} KB)...")

        try:
            # Mở font
            font = TTFont(ttf_file)
            
            # Lưu lại dưới dạng WOFF2
            font.flavor = "woff2"
            font.save(woff2_file)
            font.close()

            orig_kb = ttf_file.stat().st_size // 1024
            woff2_kb = woff2_file.stat().st_size // 1024
            print(f"  ✅ Tạo thành công {woff2_file.name}: {orig_kb} KB ➔ {woff2_kb} KB")

        except Exception as e:
            print(f"  ❌ Lỗi: {e}")

if __name__ == "__main__":
    convert_ttf_to_woff2("app/static/fonts")