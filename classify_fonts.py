import os
import shutil
import json
import time
from fontTools.ttLib import TTFont

def get_font_category(font_path, filename):
    """
    Phân loại font dựa trên:
    1. Tên file (Hỗ trợ Tiếng Việt, Tiếng Anh, Tiếng Trung)
    2. Dữ liệu kỹ thuật bên trong font (bảng post, OS/2, name)
    - Khai: Sans-Serif / Khải thể / Không phân định được
    - Thao: Serif / Tống thể / Minh thể (Có chân)
    - Trien: Monospace / Lập trình / Triện thư
    - Hanh: Script / Viết tay / Thư pháp / Thảo thư / Hành thư
    - Le: Decorative / Display / Trang trí / Nghệ thuật
    """
    fname_lower = filename.lower()
    
    # --- TẦNG 1: KIỂM TRA TỪ KHÓA TRONG TÊN FILE (Ưu tiên nhanh cho Tiếng Việt, Anh, Trung) ---
    
    # 1. Nhóm Trien (Monospace / Triện thư / Lập trình)
    if any(k in fname_lower for k in ['mono', 'code', 'consola', 'courier', 'fixed', '篆', 'zhuǎn', 'triện']):
        return 'Trien'
        
    # 2. Nhóm Hanh (Viết tay / Thư pháp / Thảo thư / Hành thư)
    if any(k in fname_lower for k in ['script', 'hand', 'brush', 'calligraphy', 'signature', 'viet_tay', 'thư_pháp', '草', '行', '手写', '书法', 'cảo', 'hành', 'thảo']):
        return 'Hanh'
        
    # 3. Nhóm Thao (Serif / Có chân / Tống thể / Minh thể)
    if any(k in fname_lower for k in ['serif', 'times', 'georgia', 'garamond', 'roman', 'minh', 'tống', '宋', '明', 'serif']):
        return 'Thao'
        
    # 4. Nhóm Le (Trang trí / Nghệ thuật / Display)
    if any(k in fname_lower for k in ['display', 'decor', 'comic', 'stencil', 'art', 'trang_tri', 'nghệ_thuật', '艺术', '装饰', '创意', '卡通']):
        return 'Le'
        
    # 5. Nhóm Khai (Sans-Serif / Khải thể / Không chân)
    if any(k in fname_lower for k in ['sans', 'gothic', 'helvetica', 'arial', 'futura', 'khai', 'kai', '黑', '楷', 'khải']):
        return 'Khai'

    # --- TẦNG 2: KIỂM TRA DỮ LIỆU KỸ THUẬT BÊN TRONG FONT (Fonttools Metadata) ---
    try:
        font = TTFont(font_path)
        
        # Kiểm tra Monospace (Nhóm Trien)
        if 'post' in font and font['post'].isFixedPitch:
            return 'Trien'
        
        # Đọc tên Family Name bên trong file font
        family_name = ""
        if 'name' in font:
            for record in font['name'].names:
                if record.nameID == 1:
                    try:
                        family_name = record.toUnicode().lower()
                        break
                    except:
                        pass
        
        # Kiểm tra thông số PANOSE từ bảng 'OS/2'
        if 'OS/2' in font:
            os2 = font['OS/2']
            if hasattr(os2, 'panose'):
                panose = os2.panose
                if len(panose) >= 2:
                    family_type = panose[0] 
                    serif_style = panose[1]
                    
                    if family_type == 3: # Chữ viết tay -> Hanh
                        return 'Hanh'
                    elif family_type == 4: # Trang trí -> Le
                        return 'Le'
                    elif family_type == 2:
                        if serif_style in [2, 3, 4, 5, 6, 7]: 
                            return 'Thao' # Serif
                        elif serif_style in [8, 9, 10, 11, 12, 13, 14, 15]: 
                            return 'Khai' # Sans-Serif

        # Fallback qua Family Name bên trong metadata
        if family_name:
            if any(k in family_name for k in ['mono', 'code', 'consola', 'courier', 'fixed', '篆']):
                return 'Trien'
            elif any(k in family_name for k in ['sans', 'gothic', 'helvetica', 'arial', 'futura', '楷', 'kai']):
                return 'Khai'
            elif any(k in family_name for k in ['script', 'hand', 'brush', 'calligraphy', 'signature', '草', '行']):
                return 'Hanh'
            elif any(k in family_name for k in ['serif', 'times', 'georgia', 'garamond', 'roman', '宋', '明']):
                return 'Thao'
            elif any(k in family_name for k in ['display', 'decor', 'comic', 'stencil', 'art', '艺术']):
                return 'Le'

    except Exception as e:
        print(f"Lỗi khi đọc file {font_path}: {e}")
        
    # Yêu cầu: Loại nào không phân định được thì lưu vào Khai
    return 'Khai'

def organize_and_generate_json(source_dir):
    categories = ['Khai', 'Thao', 'Trien', 'Hanh', 'Le']
    
    # Tạo các thư mục phân loại nếu chưa tồn tại
    for cat in categories:
        cat_path = os.path.join(source_dir, cat)
        os.makedirs(cat_path, exist_ok=True)
        
    # Duyệt qua tất cả file trong thư mục nguồn
    for filename in os.listdir(source_dir):
        font_path = os.path.join(source_dir, filename)
        
        if os.path.isfile(font_path) and filename.lower().endswith('.ttf'):
            # Truyền cả đường dẫn file và tên file vào hàm phân loại
            category = get_font_category(font_path, filename)
            
            dest_dir = os.path.join(source_dir, category)
            dest_path = os.path.join(dest_dir, filename)
            
            # Tránh ghi đè nếu trùng tên file ở thư mục đích
            if os.path.exists(dest_path) and os.path.abspath(font_path) != os.path.abspath(dest_path):
                base, ext = os.path.splitext(filename)
                dest_path = os.path.join(dest_dir, f"{base}_{int(time.time())}{ext}")
                
            # Di chuyển file
            if os.path.abspath(font_path) != os.path.abspath(dest_path):
                shutil.move(font_path, dest_path)
                print(f"Đã di chuyển: {filename} ➔ {category}/")

    # Tạo file JSON kết quả đúng mẫu
    fallback_string = "\"Noto Serif TC\", \"Noto Sans TC\", \"Noto Serif CJK TC\", \"Source Han Serif\", \"MS Mincho\", serif"
    result_dict = {}
    
    mapping_keys = {
        'Khai': 'default',
        'Le': 'le',
        'Hanh': 'hanh',
        'Thao': 'thao',
        'Trien': 'trien'
    }
    
    labels_map = {
        'default': 'Khải',
        'le': 'Lệ',
        'hanh': 'Hành',
        'thao': 'Thảo',
        'trien': 'Triện'
    }
    
    families_map = {
        'default': 'han-khai',
        'le': 'han-le',
        'hanh': 'han-hanh',
        'thao': 'han-thao',
        'trien': 'han-trien'
    }

    for cat_folder, json_key in mapping_keys.items():
        cat_path = os.path.join(source_dir, cat_folder)
        file_list = []
        
        if os.path.exists(cat_path):
            for f in os.listdir(cat_path):
                if f.lower().endswith('.ttf'):
                    file_list.append(f"{cat_folder}/{f}")
                    
        result_dict[json_key] = {
            "label": labels_map[json_key],
            "family": families_map[json_key],
            "fallback": fallback_string,
            "files": file_list
        }

    json_output_path = os.path.join(source_dir, "fonts_config.json")
    with open(json_output_path, 'w', encoding='utf-8') as json_file:
        json.dump(result_dict, json_file, ensure_ascii=False, indent=2)
        
    print(f"\nĐã xuất file cấu hình JSON thành công tại: {json_output_path}")

if __name__ == "__main__":
    target_directory = r"/Users/dustin/Desktop/looking-han-nom/app/static/fonts/NewFonts" 
    
    if os.path.exists(target_directory):
        print(f"Bắt đầu quét và phân loại font tại: {target_directory}")
        organize_and_generate_json(target_directory)
        print("Hoàn tất!")
    else:
        print(f"Thư mục {target_directory} không tồn tại.")