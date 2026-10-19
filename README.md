# arXiv Project

Đồ án *Nhập môn Khoa học dữ liệu* (HCMUS): thu thập (crawl), phân tích cấu trúc (parse) và ghép
trích dẫn (reference matching) cho các bài báo arXiv.

- **Báo cáo (Jupyter Book):** https://ahtuan171.github.io/arXiv-Project/
- **Đề bài:** https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/

## Cấu trúc repo

```
src/            Mã nguồn pipeline (config.py, utils.py, các module crawl/parse/match)
notebooks/      Notebook khám phá, thử nghiệm
book/           Jupyter Book (báo cáo M1 + M2); hình và bảng sinh ra lúc build
tests/          Unit test (pytest)
ranges.yaml     Dải arXiv ID của từng thành viên
data/           Dữ liệu cục bộ — KHÔNG commit (đã .gitignore)
.github/        GitHub Actions: build và deploy book lên GitHub Pages
```

## Bắt đầu

```bash
git clone https://github.com/ahtuan171/arXiv-Project.git
cd arXiv-Project
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q
```

Mỗi thành viên crawl **dải ID riêng** của mình:

1. Điền dải của bạn vào `ranges.yaml` (tìm theo MSSV trên sheet đăng ký, cột H–K).
2. Chọn dải bằng biến môi trường: `export ARXIV_MEMBER=member3`.
3. Dữ liệu ghi vào `data/raw/` (đổi thư mục bằng `ARXIV_DATA_DIR`).

Quy tắc dữ liệu chung:

- File trung gian và đầu ra dùng JSON Lines (`.jsonl`), UTF-8, một object mỗi dòng.
- Giá trị thiếu: chuỗi → `""`, danh sách → `[]`, số → `null`.
- Mọi arXiv ID trong dữ liệu và tên file đi qua `src.utils.clean_arxiv_id()`.
- Nghỉ tối thiểu 3 giây giữa hai lần gọi arXiv (`utils.RateLimiter`); scraper phải chạy lại được từ chỗ dừng (`utils.load_done_ids`).
- Đo hiệu năng bằng `utils.measure` / `@utils.timed` (thời gian, RAM, disk); số liệu chính thức đo trên Google Colab chỉ CPU.

## Build báo cáo

```bash
pip install -r book/requirements.txt
jupyter-book build book          # kết quả: book/_build/html/index.html
```

Push lên `main` sẽ tự build và deploy lên GitHub Pages (Settings → Pages → Source = GitHub Actions).

## Quy trình làm việc

- Không commit trực tiếp lên `main`: tạo nhánh `ten-ban/ten-viec`, mở Pull Request, ít nhất một người review.
- Commit nhỏ, message rõ ràng; lịch sử commit thể hiện đóng góp của từng người.
- Chạy `python -m pytest -q` trước khi mở PR.
- Code có type hints và docstring.

## Nhóm

| Họ tên | MSSV | Vai trò |
|---|---|---|
| Phạm Đắc Anh | 24120259 | Nhóm trưởng |
| Phạm Anh Tuấn | 24120238 | Thành viên |
| Nguyễn Duy Hào | 24120306 | Thành viên |
| Lý Phương Toàn | 24120231 | Thành viên |
| Phạm Xuân Duy | 24120295 | Thành viên |
