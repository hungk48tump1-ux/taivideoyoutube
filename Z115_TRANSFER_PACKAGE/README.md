# Gemini TXT Chunk Sender

Ứng dụng desktop Windows tự động gửi nội dung từ file `.txt` lên Gemini theo từng đợt (chunk), dùng Playwright để điều khiển trình duyệt Chrome và giao diện Tkinter.

---

## 📋 Tính năng

- Chọn file `.txt` bất kỳ từ máy
- Tự động chia file thành các chunk:
  - Chunk 1: **30 dòng** đầu (có thể đổi)
  - Chunk 2 trở đi: mỗi chunk **50 dòng** (có thể đổi)
- Tự mở Gemini bằng Chrome có **persistent profile** (chỉ đăng nhập 1 lần)
- Tự chọn model **Pro** trước mỗi lần gửi
- Paste nội dung vào ô chat → Gửi → Chờ Gemini trả lời xong → Tiếp chunk sau
- Lưu **prompt + response + metadata** cho mỗi chunk
- Hỗ trợ **Pause / Resume / Stop**
- Hỗ trợ **Resume** sau khi app bị tắt giữa chừng
- **Dry Run**: chỉ chia chunk, không gửi Gemini, xuất file để kiểm tra
- **Test Selectors**: kiểm tra selector nào còn hoạt động
- Log chi tiết ra file trong thư mục `logs/`

---

## ⚙️ Yêu cầu

- **Windows 10/11**
- **Python 3.11+** — [Tải tại đây](https://www.python.org/downloads/)
- **Google Chrome** đã cài trên máy
- Tài khoản Google đã đăng nhập Gemini (sẽ lưu vào profile)

---

## 🚀 Cài đặt

### Bước 1: Cài Python
Tải Python 3.11+ từ https://python.python.org/downloads/ và chọn **"Add Python to PATH"** khi cài.

### Bước 2: Clone / Giải nén project

```
gemini-txt-sender/
├── main.py
├── requirements.txt
├── config/
│   ├── config.json
│   └── selectors.json
├── core/
│   ├── file_loader.py
│   ├── chunker.py
│   ├── state_manager.py
│   └── logger.py
├── automation/
│   └── gemini_controller.py
├── ui/
│   └── app_ui.py
├── outputs/   (tự tạo)
└── logs/      (tự tạo)
```

### Bước 3: Cài dependencies

Mở Command Prompt hoặc PowerShell trong thư mục project:

```powershell
pip install -r requirements.txt
playwright install
```

> Lệnh `playwright install` sẽ tải trình duyệt Chromium về (≈200MB).
> Nếu bạn muốn dùng Chrome thực thay vì Chromium, đảm bảo Google Chrome đã được cài.

### Bước 4: Chạy ứng dụng

```powershell
python main.py
```

---

## 🔐 Đăng nhập Gemini lần đầu

1. Nhấn nút **"🚀 Khởi Động Gemini"** trong app
2. Một cửa sổ Chrome sẽ mở ra với trang Gemini
3. **Đăng nhập bằng tài khoản Google của bạn** qua cửa sổ Chrome đó
4. Sau khi đăng nhập thành công và thấy giao diện chat, app sẽ ghi nhớ profile
5. Từ lần sau, ấn "Khởi Động Gemini" sẽ tự động vào Gemini mà không cần login lại

> **Profile được lưu tại:** `browser_profile/` trong thư mục project.
> Xóa thư mục này nếu muốn đăng nhập lại tài khoản khác.

---

## 📖 Hướng dẫn sử dụng

1. **Chọn file TXT**: Nhấn `...` hoặc gõ đường dẫn trực tiếp
2. **Cấu hình chunk**: Điều chỉnh số dòng cho chunk 1 và các chunk sau
3. **Cấu hình model**: Nhập tên model (mặc định `Pro`, app sẽ tìm model có chứa chữ này)
4. **Khởi Động Gemini**: Mở browser và vào trang chat
5. **Bắt Đầu Chạy**: Bắt đầu gửi chunk. App sẽ tự:
   - Chọn model Pro
   - Paste nội dung chunk vào ô chat
   - Nhấn gửi
   - Chờ Gemini trả lời xong
   - Lưu kết quả
   - Nghỉ vài giây rồi gửi chunk tiếp
6. **Tạm Dừng / Tiếp Tục**: Dừng sau chunk hiện tại hoặc tiếp tục
7. **Dừng Hẳn**: Dừng hoàn toàn

---

## 📁 Kết quả lưu ở đâu?

Mỗi phiên chạy tạo một thư mục riêng trong `outputs/`:

```
outputs/
└── session_20250116_143022/
    ├── session_state.json       ← trạng thái phiên, dùng để resume
    ├── 001_prompt.txt           ← nội dung chunk 1 đã gửi
    ├── 001_response.txt         ← response từ Gemini
    ├── 001_meta.json            ← metadata (thời gian, model, dòng...)
    ├── 002_prompt.txt
    ├── 002_response.txt
    ├── 002_meta.json
    └── ...
```

---

## 🔄 Resume sau khi app bị tắt

Nếu app bị đóng giữa chừng, lần mở lại:
1. Chọn lại file TXT cũ
2. Nhấn "Bắt Đầu Chạy"
3. App đọc `session_state.json` và hỏi bạn muốn tiếp tục từ chunk chưa xong hay chạy lại từ đầu

> **Lưu ý**: Tính năng resume đầy đủ cần chỉ cùng một output session. Nếu bạn xóa thư mục `outputs/session_*` thì không resume được.

---

## 🛠️ Sửa selector khi Gemini đổi giao diện

Khi Gemini cập nhật UI, các selector CSS có thể không còn đúng. Hãy sửa file:

```
config/selectors.json
```

Mỗi entry có dạng:

```json
"chat_input": {
  "_description": "Ô nhập chat chính",
  "selectors": [
    "div[contenteditable='true'][data-placeholder]",
    "rich-textarea div[contenteditable='true']",
    "..."
  ]
}
```

**Cách tìm selector đúng:**
1. Mở Gemini trong Chrome
2. Nhấn F12 mở DevTools
3. Nhấn Ctrl+Shift+C (chọn element)
4. Click vào element bạn muốn
5. Chuột phải vào element trong DevTools → Copy → Copy selector
6. Paste vào đầu danh sách selectors tương ứng trong `selectors.json`
7. Nhấn nút **"Test Selectors"** trong app để kiểm tra

---

## ⚙️ Cấu hình nâng cao (config.json)

| Key | Mặc định | Mô tả |
|-----|----------|-------|
| `gemini_url` | `https://gemini.google.com/app` | URL trang chat |
| `target_model_text` | `"Pro"` | Chữ cần có trong tên model |
| `first_chunk_size` | `30` | Số dòng chunk đầu |
| `next_chunk_size` | `50` | Số dòng các chunk sau |
| `skip_empty_lines` | `true` | Bỏ qua dòng trống |
| `inter_chunk_delay_seconds` | `3` | Giây nghỉ giữa các chunk |
| `response_timeout_seconds` | `300` | Timeout chờ Gemini (giây) |
| `stable_wait_seconds` | `5` | Chờ response ổn định (giây) |
| `browser_channel` | `"chrome"` | `"chrome"` hoặc `"chromium"` |
| `user_data_dir` | `browser_profile` | Thư mục lưu profile Chrome |
| `random_delay_min` | `0.5` | Delay ngẫu nhiên tối thiểu (giây) |
| `random_delay_max` | `1.5` | Delay ngẫu nhiên tối đa (giây) |

---

## 🐛 Xử lý sự cố thường gặp

### App không paste được vào ô chat
- Thử nhấn vào ô chat tay một lần, rồi nhấn Bắt Đầu Chạy lại
- Kiểm tra selector `chat_input` trong `selectors.json`

### Không tìm thấy model Pro
- Mở Gemini thủ công, xem tên model hiển thị chính xác là gì
- Sửa `target_model_text` trong `config.json` cho khớp

### Gemini không phản hồi / timeout
- Tăng `response_timeout_seconds` trong `config.json` (mặc định 300 giây)
- Kiểm tra kết nối internet
- Gemini đôi khi bị throttle khi nhận quá nhiều request

### Lỗi "playwright not found"
```powershell
pip install playwright
playwright install
```

### Chrome không mở được
- Đảm bảo Google Chrome đã cài tại vị trí mặc định
- Thử đổi `browser_channel` thành `"chromium"` trong `config.json`

---

## 📝 License

MIT License — Sử dụng tự do, không bảo đảm.
