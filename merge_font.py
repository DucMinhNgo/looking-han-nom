# -*- coding: utf-8 -*-
from pathlib import Path
import subprocess
import shutil

def merge_with_fontforge(ttf_files: list[Path], output_file: Path, family_name: str) -> bool:
    if not ttf_files:
        return False

    if len(ttf_files) == 1:
        shutil.copy2(ttf_files[0], output_file)
        print(f"  ✅ Chỉ 1 file → copy: {ttf_files[0].name}")
        return True

    pe_script = f'Open("{ttf_files[0].as_posix()}");\n'
    for f in ttf_files[1:]:
        pe_script += f'MergeFonts("{f.as_posix()}");\n'

    pe_script += f'''
SetFontNames("{family_name}", "{family_name}", "{family_name}", "Regular", "Merged");
SetTTFName(0x409, 1, "{family_name}");
SetTTFName(0x409, 2, "Regular");
SetTTFName(0x409, 4, "{family_name}");
SetTTFName(0x409, 16, "{family_name}");
SetTTFName(0x409, 17, "Regular");
Generate("{output_file.as_posix()}", "", 0x84);
Quit();
'''

    script_path = output_file.parent / "_merge_temp.pe"
    script_path.write_text(pe_script, encoding="utf-8")

    try:
        result = subprocess.run(
            ["fontforge", "-lang=ff", "-script", str(script_path)],
            capture_output=True,
            text=True,
            timeout=180
        )
        script_path.unlink(missing_ok=True)

        if output_file.exists() and output_file.stat().st_size > 5000:
            size_kb = output_file.stat().st_size // 1024
            print(f"  ✅ Đã tạo: {output_file.name} ({size_kb} KB)")
            return True
        else:
            print("  ❌ Không tạo được file hợp lệ")
            if result.stderr:
                print(result.stderr[:600])
            return False
    except FileNotFoundError:
        print("  ❌ Vẫn chưa có FontForge. Chạy: brew install fontforge")
        return False
    except Exception as e:
        print(f"  ❌ Lỗi: {e}")
        return False


def process_all_folders(source_dir="font", output_dir="tff"):
    source = Path(source_dir).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)

    subfolders = sorted([d for d in source.iterdir() if d.is_dir()])
    print(f"📂 Tìm thấy {len(subfolders)} thư mục con\n")

    success = 0
    for folder in subfolders:
        out_name = folder.name.lower() + ".ttf"
        out_path = output / out_name
        family = folder.name.capitalize()

        ttf_files = sorted([
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in {".ttf", ".tff"}
        ])

        print(f"▶ Xử lý: {folder.name}/ → {out_name}")
        print(f"  🔄 Gộp {len(ttf_files)} file...")
        for f in ttf_files:
            print(f"      - {f.name}")

        if merge_with_fontforge(ttf_files, out_path, family):
            success += 1
        print()

    print(f"🎉 Xong! Tạo được {success}/{len(subfolders)} font trong: {output}")


if __name__ == "__main__":
    process_all_folders("font", "app/static/fonts")