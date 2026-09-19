# Tra cứu Hán-Nôm

Công cụ tra cứu trên một bộ dataset ground truth đã hoàn thiện. Một ô tìm kiếm:
dán link Facebook, post id, một đoạn caption, hay vài chữ bạn nhớ mang máng —
nhận lại ảnh, caption, phần phiên âm, và nút mở đúng bài post đó.

Chỉ đọc. Dataset và thư mục ảnh được mount từ máy host; app không bao giờ ghi
vào chúng.

```
dataset.jsonl  ─┐
                ├─►  tìm kiếm  ─►  ảnh + caption + ground truth + link bài post
thư mục ảnh/   ─┘
```

---

## Chạy thử trong 2 phút

Repo đã kèm sẵn bộ mẫu 10 item, nên **không cần chuẩn bị dữ liệu gì cả**.

### Cách A — không cần Docker

```bash
cd hannom-app
python -m venv .venv
```

Kích hoạt môi trường ảo:

```bash
.venv\Scripts\activate
```

*(macOS/Linux: `source .venv/bin/activate`)*

```bash
pip install -r requirements.txt
```

Tạo file cấu hình — lệnh này tự sinh khoá bí mật và hỏi bạn đặt mật khẩu:

```bash
python -m app.cli --init-env
```

Chạy app:

```bash
uvicorn app.api.main:app --port 8000
```

Mở **http://127.0.0.1:8000**, đăng nhập bằng `admin` và mật khẩu bạn vừa đặt.

### Cách B — Docker

```bash
cd hannom-app
python -m app.cli --init-env
docker compose up -d --build
```

Mở **http://127.0.0.1:8000**. Xem log bằng `docker compose logs -f lookup`.

> `--init-env` cần Python trên máy. Nếu không có, copy `.env.example` thành
> `.env` rồi tự điền `AUTH_SECRET` và `APP_PASSWORD_HASH` — cách sinh hai giá
> trị đó có ghi ngay trong file.

Dù chạy cách nào, bạn cũng sẽ thấy ngay màn hình này: 10 dòng, 10 ảnh, và ba con
số lệch nhau (9 khớp / 1 dòng thiếu ảnh / 1 ảnh thừa). **Bộ mẫu cố ý lệch** để
bạn thấy cơ chế đối chiếu hoạt động, chứ không phải một màn hình sạch đáng ngờ.

---

## Dùng dữ liệu của bạn

Dataset là một file JSONL, mỗi dòng một ảnh:

```jsonc
{
  "image": "sample_01.jpg",
  "post_id": "https://www.facebook.com/permalink.php?story_fbid=…&id=…",
  "caption": "Bốn câu thơ về thời gian trôi",
  "ground_truth": "花開花落又一秋\n時光匆匆似水流\n…"
}
```

`post_id` ở đây đã là URL, nên nút **Mở bài post** dùng thẳng nó. Giá trị không
phải URL vẫn hiển thị nhưng không biến thành link — một cái nút bấm vào không
đi đâu thì tệ hơn là không có nút.

Tên cột được chấp nhận linh hoạt (`img`, `post_url`, `fb_caption`, `gt`,
`label`…), và cột lạ nào khác cũng được giữ lại chứ không bị bỏ. Dòng hỏng thì
bỏ qua và **đếm lại**, chứ không làm hỏng cả file.

**Không Docker** — sửa `.env`:

```bash
DATASET_PATH=D:/du-lieu/ground_truth.jsonl
IMAGES_DIR=D:/du-lieu/images
```

**Có Docker** — thư mục của bạn cần có `dataset.jsonl` và `images/` nằm cạnh
nhau, rồi sửa một dòng trong `docker-compose.yml`:

```yaml
volumes:
  - ./data:/data
  - ./du-lieu:/srv/src:ro      # thay ./sample bằng thư mục của bạn
```

---

## Thêm ảnh, thêm dữ liệu sau này

**Không cần khởi động lại.** Cả dataset lẫn thư mục ảnh đều được đọc lại khi
thay đổi. Cụ thể từng trường hợp — đều đã được kiểm chứng bằng test:

| Bạn làm gì | Thấy ngay? |
|---|---|
| Thêm dòng vào `dataset.jsonl` | Ngay lập tức |
| Sửa/thay cả file `dataset.jsonl` | Ngay lập tức |
| Copy ảnh vào **thẳng** thư mục ảnh | Ngay lập tức |
| Xoá ảnh | Ngay lập tức |
| Copy ảnh vào **thư mục con** | Trong vòng 30 giây, hoặc bấm **Quét lại ảnh** |
| Thay một ảnh bằng ảnh khác **cùng tên** | Trong vòng 30 giây, hoặc **Quét lại ảnh** |
| Tạo dataset/thư mục ảnh sau khi app đã chạy | Ngay lập tức |

Hai dòng "trong vòng 30 giây" khác phần còn lại vì cùng một lý do: hệ điều hành
chỉ đổi mtime của thư mục **trực tiếp** chứa file, và thay file cùng tên thì
không đổi gì ở mức thư mục cả. App phát hiện thay đổi bằng mtime **cộng với số
entry** của thư mục gốc — chỉ mtime là không đủ, vì timestamp trên Windows rơi
vào tick ~15 ms nên xoá file ngay sau lần quét trước sẽ tàng hình. Hai trường
hợp còn lại được chặn bằng thời gian, và nút **Quét lại ảnh** bỏ qua chờ đợi.

Chi phí, đo trên thư mục 9.000 ảnh: mỗi request tốn ~15 ms để kiểm tra thay
đổi, và khi thật sự phải quét lại thì hết ~55 ms.

Điều dễ chịu nhất: thêm ảnh còn thiếu thì **dòng "thiếu ảnh" tự chuyển thành
"khớp"** — không phải làm gì thêm, ba con số ở đầu trang tự cập nhật.

### Một cái bẫy của Docker đã được tránh sẵn

`docker-compose.yml` mount **cả thư mục**, không mount từng file:

```yaml
- ./sample:/srv/src:ro          # đúng
# - ./sample/dataset.jsonl:/srv/src/dataset.jsonl:ro    # SAI
```

Bind-mount một file đơn lẻ sẽ ghim inode của nó. Hầu hết trình soạn thảo lưu
file bằng cách ghi ra file tạm rồi đổi tên đè lên — lúc đó container vẫn nhìn
vào file cũ, và bạn sẽ sửa dataset mà không thấy gì thay đổi cho tới khi
recreate container. Mount thư mục thì không dính bẫy này.

---

## Khi dữ liệu và ảnh lệch nhau

Chắc chắn sẽ lệch. Dataset và thư mục ảnh được tạo ra riêng rẽ rồi trôi khỏi
nhau, nên app **thiết kế cho việc đó** thay vì coi là lỗi. Không có gì bị âm
thầm bỏ đi:

| | Nghĩa là gì | Bạn thấy gì |
|---|---|---|
| **Khớp ảnh** | dòng và file tìm thấy nhau | thẻ bình thường |
| **Dòng thiếu ảnh** | dòng trỏ tới ảnh không có trong thư mục | thẻ viền nét đứt — caption và ground truth vẫn đọc và tìm kiếm được |
| **Ảnh thừa** | file không dòng nào nhận | liệt kê theo tên ở mục riêng |

Ba con số nằm ngay đầu trang, kèm nút lọc cho loại thứ hai và danh sách cho
loại thứ ba.

**Ghép ảnh khoan dung** theo 4 bậc giảm dần: tên y hệt → chỉ basename → không
phân biệt hoa/thường → bỏ phần mở rộng. Bốn bậc này phủ đúng những chỗ lệch
thật sự hay xảy ra — tiền tố `images/` bị cắt, `.JPG` thành `.jpg`, `.jpeg`
thành `.jpg`. Lỏng hơn nữa là bắt đầu ghép nhầm ảnh vào chữ, còn tệ hơn báo
thiếu.

Thư mục con cũng được quét, và hai dòng dùng chung một ảnh không bị tính là lỗi.

---

## Tìm kiếm

Substring không phân biệt hoa/thường — đây là lựa chọn đúng chứ không phải làm
tắt: nội dung là Hán-Nôm, không có khoảng trắng để tách từ, nên index theo từ sẽ
không khớp được thứ người đọc mong đợi, trong khi substring theo ký tự đúng là
cách người ta tìm một câu nhớ mang máng.

- Chuẩn hoá NFC trước, nên chữ dạng tổ hợp và dạng dựng sẵn vẫn khớp nhau.
- Gõ **chỉ dãy số** của post vẫn tìm ra dù dữ liệu lưu URL đầy đủ — không ai
  phải dán cả cái link.
- Dropdown thu hẹp về một trường: link/post id, caption, ground truth, tên file.
- Kết quả khớp được **tô sáng**, nên một câu trúng giữa bài thơ dài vẫn thấy
  ngay bằng mắt.

---

## Tài khoản

Mọi thứ nằm sau đăng nhập, **kể cả ảnh**. Tài khoản quản trị gốc lấy từ biến môi
trường (`APP_USERNAME`, `APP_PASSWORD_HASH`) và không thể tạo, đổi tên hay khoá
qua giao diện — để một sai sót trong file tài khoản không bao giờ khoá được tất
cả mọi người ra ngoài. Người dùng khác do admin thêm, lưu ở `data/users.json`
dưới dạng bcrypt hash.

---

## Gặp trục trặc

| Hiện tượng | Nguyên nhân |
|---|---|
| `Refusing to start: AUTH_SECRET … not set` | Chưa có `.env`. Chạy `python -m app.cli --init-env` |
| Đăng nhập xong lại quay về trang login | `COOKIE_SECURE=1` nhưng đang chạy HTTP thường. Cookie `Secure` không bao giờ được gửi qua http — đặt `COOKIE_SECURE=0` khi chạy local |
| `port is already allocated` | Cổng 8000 đang bận. Đổi `--port 8001`, hoặc sửa phần `ports` trong compose |
| Mọi dòng đều báo thiếu ảnh | `IMAGES_DIR` trỏ sai chỗ. Kiểm tra bằng `python -m app.cli --check` |
| Copy ảnh mới vào mà không thấy | Nếu ảnh nằm trong thư mục con: đợi 30 giây hoặc bấm **Quét lại ảnh**. Nếu chạy Docker và mount từng file: xem mục bẫy Docker ở trên |
| Trang trống, 0 dòng | `DATASET_PATH` trỏ sai, hoặc file rỗng. `--check` sẽ ghi `MISSING` |
| Sửa `.env` rồi mà Docker không đổi | `env_file` chỉ đọc lúc tạo container. Chạy `docker compose up -d --force-recreate` |

Lệnh chẩn đoán đầu tiên nên chạy, luôn luôn:

```bash
python -m app.cli --check
```

Nó in ra đường dẫn dataset, đường dẫn ảnh, số dòng, số file, ba con số đối
chiếu, và secret nào còn thiếu.

---

## Dòng lệnh

Chạy được mà không cần tầng web — `app/core/` không bao giờ import FastAPI, và
`--check` chứng minh điều đó bằng cách import mọi module core.

```bash
python -m app.cli --init-env           # lần đầu: sinh .env chạy được ngay
python -m app.cli --check              # cấu hình, số liệu, secret
python -m app.cli --stats              # như trên, dạng JSON
python -m app.cli --mismatch           # liệt kê chính xác chỗ nào lệch
python -m app.cli --search "落又一"
python -m app.cli --add-user mai --password '…'
python -m app.cli --hash-password      # chỉ in hash, không ghi file
```

---

## Cấu trúc

```
app/core/            tuyệt đối không có web framework — app/cli.py chứng minh
  dataset.py           nạp JSONL, tìm kiếm
  localimages.py       ghép dòng với file; ba nhóm lệch
  models.py            một dòng dữ liệu, và phần text dùng để so khớp
  users.py             tài khoản
app/api/             lớp FastAPI bọc bên ngoài
app/static/           giao diện
sample/               bộ 10 item mà compose phục vụ sẵn
scripts/              sinh lại bộ mẫu đó
```

## Test

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

89 test, không cần Docker, không cần dữ liệu thật.
