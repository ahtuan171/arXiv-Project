# arXiv Project

Đồ án *Nhập môn Khoa học dữ liệu* (HCMUS): thu thập (crawl), phân tích cấu trúc (parse) và ghép
trích dẫn (reference matching) cho các bài báo arXiv.

- **Báo cáo (Jupyter Book):** https://ahtuan171.github.io/arXiv-Project/
- **Đề bài:** https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/

## Cấu trúc repo

```
src/                Mã nguồn pipeline
  config.py         Cấu hình đường dẫn, rate limit, dải arXiv ID
  crawler.py        Crawler có resume, chọn client API/browser
  s2_api_client.py  Client Graph API chính thức của Semantic Scholar
  s2_client.py      Client Playwright (fallback / đối chiếu)
  stats.py          Thống kê và báo cáo Markdown
  read_ref.py       Đọc references.jsonl -> pandas DataFrame
  utils.py          JSONL, rate limiter, retry, logging, đo hiệu năng
scripts/            CLI (crawl_references.py)
notebooks/          Notebook khám phá, thử nghiệm
book/               Jupyter Book (báo cáo M1 + M2)
docs/               Tài liệu chi tiết (crawler, dữ liệu)
tests/              Unit test (pytest)
statistics/         Thống kê lần chạy gần nhất của từng member (.json, .md)
ranges.yaml         Dải arXiv ID của từng thành viên
data/               Dữ liệu cục bộ — KHÔNG commit (đã .gitignore)
local/              Ghi chú riêng — KHÔNG commit
.github/            GitHub Actions: build và deploy book lên GitHub Pages
```

## Bắt đầu

Yêu cầu Python 3.10+.

```bash
git clone https://github.com/ahtuan171/arXiv-Project.git
cd arXiv-Project
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q
```

Mỗi thành viên crawl **dải ID riêng** của mình:

1. Điền dải của bạn vào `ranges.yaml` (tìm theo MSSV trên sheet đăng ký, cột H–K).
   Nếu dải vắt qua tháng, điền thêm `month_sizes` (ID lớn nhất của tháng trước).
2. Chọn dải bằng biến môi trường: `export ARXIV_MEMBER=member3`
   (hoặc truyền `--member member3`).
3. Chạy crawler:

```bash
python scripts/crawl_references.py                # Graph API (mặc định), tự resume
python scripts/crawl_references.py --dry-run      # xem còn bao nhiêu paper
python scripts/crawl_references.py --limit 20     # thử nhanh 20 paper
```

Dữ liệu ghi riêng vào `data/raw/<member>/` (`references.jsonl`, `ledger.jsonl`);
log và thống kê nằm trong `logs/` và `statistics/`.

## Cào metadata references

Hướng dẫn chi tiết về crawler: chọn client (`api` / `browser` / `both`), fallback
và retry, resume, exit code, schema `references.jsonl` / `ledger.jsonl`, đọc dữ
liệu bằng pandas và xử lý sự cố.

Xem [docs/crawl-references.md](docs/crawl-references.md).

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
