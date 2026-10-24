import os
import sys
import json
import argparse

# ============================================================
# CẤU HÌNH ĐƯỜNG DẪN MẶC ĐỊNH
# ============================================================
DEFAULT_FONTS_DIR = "/Users/dustin/Desktop/looking-han-nom/app/static/fonts"
DEFAULT_OUTPUT_JSON = "/Users/dustin/Desktop/looking-han-nom/app/static/fonts/fonts.json"

ALLOWED_EXT = (".ttf", ".otf", ".woff", ".woff2")

# Danh sách cấu hình metadata theo thư mục
CONFIG = {
    "default": {
        "folder": "Khai",
        "label": "Khải",
        "family": "han-khai",
        "fallback": '"Noto Serif TC", "Noto Sans TC", "Noto Serif CJK TC", "Source Han Serif", "MS Mincho", serif'
    },
    "le": {
        "folder": "Le",
        "label": "Lệ",
        "family": "han-le",
        "fallback": '"Noto Serif TC", "Noto Sans TC", "Noto Serif CJK TC", "Source Han Serif", "MS Mincho", serif'
    },
    "hanh": {
        "folder": "Hanh",
        "label": "Hành",
        "family": "han-hanh",
        "fallback": '"Noto Serif TC", "Noto Sans TC", "Noto Serif CJK TC", "Source Han Serif", "MS Mincho", serif'
    },
    "thao": {
        "folder": "Thao",
        "label": "Thảo",
        "family": "han-thao",
        "fallback": '"Noto Serif TC", "Noto Sans TC", "Noto Serif CJK TC", "Source Han Serif", "MS Mincho", serif'
    },
    "trien": {
        "folder": "Trien",
        "label": "Triện",
        "family": "han-trien",
        "fallback": '"Noto Serif TC", "Noto Sans TC", "Noto Serif CJK TC", "Source Han Serif", "MS Mincho", serif'
    }
}

def scan_fonts(base_dir, folder_name):
    """Quét và lấy toàn bộ file font trong thư mục con tương ứng."""
    folder_path = os.path.join(base_dir, folder_name)
    if not os.path.isdir(folder_path):
        return []
    
    files = [
        f"{folder_name}/{f}"
        for f in os.listdir(folder_path)
        if f.lower().endswith(ALLOWED_EXT) and not f.startswith(".")
    ]
    return sorted(files)

def parse_arguments():
    parser = argparse.ArgumentParser(description="Quét thư mục fonts và cập nhật file JSON.")
    parser.add_argument(
        "-i", "--input", 
        dest="input_dir", 
        default=None, 
        help="Đường dẫn đến thư mục fonts (ví dụ: ./fonts hoặc D:/project/fonts)"
    )
    parser.add_argument(
        "-o", "--output", 
        dest="output_file", 
        default=None, 
        help="Đường dẫn file JSON đầu ra (ví dụ: ./fonts.json)"
    )
    return parser.parse_args()

def main():
    args = parse_arguments()

    # Nhận đường dẫn thư mục fonts
    fonts_dir = args.input_dir
    if not fonts_dir:
        user_input = input(f"Nhập đường dẫn thư mục fonts (Enter để dùng '{DEFAULT_FONTS_DIR}'): ").strip()
        fonts_dir = user_input if user_input else DEFAULT_FONTS_DIR

    # Nhận đường dẫn file JSON output
    output_file = args.output_file
    if not output_file:
        user_output = input(f"Nhập đường dẫn file JSON output (Enter để dùng '{DEFAULT_OUTPUT_JSON}'): ").strip()
        output_file = user_output if user_output else DEFAULT_OUTPUT_JSON

    fonts_dir = os.path.abspath(fonts_dir)
    output_file = os.path.abspath(output_file)

    if not os.path.exists(fonts_dir) or not os.path.isdir(fonts_dir):
        print(f"\n[Lỗi] Không tìm thấy thư mục fonts tại: {fonts_dir}")
        sys.exit(1)

    print(f"\n[*] Đang quét thư mục: {fonts_dir}")

    updated_data = {}

    for key, info in CONFIG.items():
        folder_name = info["folder"]
        
        # Chỉ lấy chính xác các file font có trong thư mục của chính nó
        # (Ví dụ: 'hanh' chỉ quét và lưu các file trong Hanh/*)
        files_list = scan_fonts(fonts_dir, folder_name)

        updated_data[key] = {
            "label": info["label"],
            "family": info["family"],
            "fallback": info["fallback"],
            "files": files_list
        }

    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(updated_data, f, ensure_ascii=False, indent=2)

    print(f"[✓] Đã xuất thành công ra file: {output_file}")

if __name__ == "__main__":
    main()