# Cào metadata references từ Semantic Scholar

Tài liệu chi tiết cho `scripts/crawl_references.py`: cấu hình, chọn client, resume,
schema dữ liệu và xử lý sự cố. Tổng quan dự án xem tại [README](../README.md).

## Mục lục

- [Cấu hình](#cấu-hình)
- [Chạy crawler](#chạy-crawler)
- [Chọn client](#chọn-client)
- [Fallback và retry](#fallback-và-retry)
- [Resume và tính bền vững](#resume-và-tính-bền-vững)
- [Exit code](#exit-code)
- [Dữ liệu đầu ra](#dữ-liệu-đầu-ra)
- [Quy tắc dữ liệu chung](#quy-tắc-dữ-liệu-chung)
- [Đọc dữ liệu](#đọc-dữ-liệu)
- [Theo dõi tiến độ](#theo-dõi-tiến-độ)
- [Xử lý sự cố](#xử-lý-sự-cố)

## Cấu hình

Yêu cầu Python 3.10+ và đã cài dependency:

```bash
pip install -r requirements.txt
playwright install chromium   # chỉ cần cho --client browser hoặc fallback
```

Tạo `.env` ở gốc repo (không commit):

```bash
S2_API_KEY=<key từ https://www.semanticscholar.org/product/api>
ARXIV_MEMBER=member3
```

`S2_API_KEY` là tuỳ chọn: không có key vẫn chạy được nhưng dùng quota ẩn danh
thấp hơn nhiều. Khi có key, crawler gửi header `x-api-key`.

Mỗi thành viên crawl **dải ID riêng** của mình:

1. Điền dải của bạn vào `ranges.yaml` (tìm theo MSSV trên sheet đăng ký, cột H–K).
   Nếu dải vắt qua tháng, điền thêm `month_sizes` (ID lớn nhất của tháng trước).
2. Chọn dải bằng biến môi trường `ARXIV_MEMBER=member3` hoặc truyền
   `--member member3`.
3. Chạy crawler (xem bên dưới).

Các biến môi trường liên quan:

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| `S2_API_KEY` | rỗng | Key Graph API; không có key thì dùng quota ẩn danh |
| `S2_API_MIN_INTERVAL` | `1.0` | Giây tối thiểu giữa hai request API, dùng chung mọi worker |
| `S2_API_REF_FIELDS` | `citedPaper.paperId,citedPaper.externalIds,citedPaper.title,citedPaper.authors,citedPaper.year,citedPaper.publicationDate` | Các trường lấy cho mỗi reference; mặc định suy ra từ `S2_API_PAPER_FIELDS` |
| `S2_WEB_MIN_INTERVAL` | `3.0` | Giây tối thiểu giữa hai request browser, dùng chung mọi tab |
| `S2_TABS` | `3` | Số worker API / số tab Chromium mặc định |
| `S2_HEADLESS` | `1` | `0`/`false` để hiện cửa sổ browser |
| `ARXIV_MEMBER` | — | Member key trong `ranges.yaml` |
| `ARXIV_DATA_DIR` | `data/` | Gốc dữ liệu, có thể đổi bằng `--data-dir` |

## Chạy crawler

```bash
python scripts/crawl_references.py                    # Graph API (mặc định), tự resume
python scripts/crawl_references.py --limit 20         # thử nhanh 20 paper
python scripts/crawl_references.py --dry-run          # xem còn bao nhiêu paper
python scripts/crawl_references.py --client browser   # scrape website bằng Chromium
python scripts/crawl_references.py --client both      # API + browser, log chỗ lệch
python scripts/crawl_references.py --api-min-interval 1.0  # giãn nhịp gọi API
python scripts/crawl_references.py --tabs 3           # số worker API / số tab Chromium
python scripts/crawl_references.py --retry-errors     # thử lại paper từng lỗi
python scripts/crawl_references.py --retry-not-found  # thử lại paper S2 chưa index
python scripts/crawl_references.py --retry-partial    # thử lại paper hoàn thành nhưng còn unresolved
python scripts/crawl_references.py --verbose          # log từng paper + từng request S2
python scripts/crawl_references.py --headed           # mở cửa sổ browser để debug
python scripts/crawl_references.py --data-dir /data/arxiv  # đổi gốc dữ liệu
python scripts/crawl_references.py --month-size 2501=24000
python scripts/crawl_references.py --tolerate-errors 5     # exit 0 nếu <= 5 lỗi
```

## Chọn client

- `--client api` (mặc định): dùng Graph API chính thức. Một `AsyncRateLimiter`
  dùng chung cho mọi worker (`S2_API_MIN_INTERVAL`, mặc định `1.0` giây/request).
  `--tabs` ở chế độ này là số worker xử lý song song.
- `--client browser`: scrape endpoint JSON của website qua Chromium (một browser,
  3 tab tái sử dụng, `--tabs` là số tab). Dùng `S2_WEB_MIN_INTERVAL` (mặc định
  `3.0` giây/request, dùng chung cho mọi tab) và nghỉ 30 giây sau WAF challenge.
- `--client both`: chạy API trước, chạy thêm browser để đối chiếu, log các cặp
  cạnh chênh lệch rồi vẫn ghi kết quả API.

## Fallback và retry

Nếu API trả 401/403 (key sai/hết quyền) hoặc 429 (hết quota), crawler tự chuyển
sang browser, giữ nguyên tiến độ và ghi `client_type = "api->browser"` trong
thống kê.

- 404: paper chưa được S2 index — ghi `not_found`, không retry.
- 429: retry theo header `Retry-After` (hỗ trợ cả giây và HTTP-date) và tạm chặn
  mọi worker qua rate limiter.
- 5xx / lỗi mạng: retry với exponential backoff + jitter.
- 401/403: không retry; kích hoạt fallback ở chế độ `--client api`.

## Resume và tính bền vững

- Mỗi paper xong được ghi ngay vào ledger (fsync), nên Ctrl-C an toàn: paper
  đang làm dở không bị đánh dấu `ok` và sẽ được làm lại ở lần chạy sau.
- Paper 0 reference, paper `not_found`, paper `partial` (có reference unresolved)
  và paper `error` đều có trạng thái riêng, tránh lặp vô hạn. Dùng
  `--retry-errors` / `--retry-not-found` / `--retry-partial` để thử lại.
- Một file lock (`.crawler.lock`) ngăn hai tiến trình cùng ghi vào một member.

## Exit code

| Mã | Ý nghĩa |
|---:|---|
| `0` | Thành công, hoặc `--dry-run` |
| `1` | Lỗi fatal (không đọc được range, không mở được browser, lock, exception) |
| `2` | Chạy xong nhưng số lỗi vượt `--tolerate-errors` (mặc định 0) |
| `130` | Bị Ctrl-C; tiến độ đã được lưu |

## Dữ liệu đầu ra

Mỗi member ghi riêng vào `data/raw/<member>/` (đổi gốc bằng `ARXIV_DATA_DIR`
hoặc `--data-dir`):

- `references.jsonl` — cạnh trích dẫn, mỗi dòng gồm `arxiv_id`,
  `ref_arxiv_id`, `ref_title`, `ref_authors`, `ref_submission_date`, `ref_s2_id`.
- `ledger.jsonl` — trạng thái từng paper (`ok` / `partial` / `not_found` / `error`,
  số lần thử, số reference tìm thấy/ghi được, và các metrics chi tiết); đây là
  cơ sở để resume.
- File cũ dùng chung `data/raw/references.jsonl` được tự động nhập một lần vào
  đúng thư mục member (theo dải ID) khi chạy lần đầu.
- Log: `logs/crawl_references_<member>.log`.
- Thống kê: `statistics/crawl_references_<member>.json` và `.md`; file JSON có
  thêm `client_type`, `api_calls`, `browser_calls`, `rate_limited`,
  `total_ids`, `already_done`, `elapsed_seconds`.

Ví dụ một dòng `references.jsonl`:

```json
{
  "arxiv_id": "2504_12523",
  "ref_arxiv_id": "2401_05432",
  "ref_title": "Attention Is All You Need",
  "ref_authors": ["Ashish Vaswani", "Noam Shazeer"],
  "ref_submission_date": "2024-01-10",
  "ref_s2_id": "abc123..."
}
```

### Ledger schema (mỗi dòng một paper)

```json
{
  "schema_version": 2,
  "arxiv_id": "2504_12523",
  "member": "member3",
  "status": "ok",
  "attempts": 0,
  "refs_total": 45,
  "refs_with_arxiv": 38,
  "refs_written": 38,
  "raw_refs_received": 45,
  "refs_with_paper_id": 42,
  "refs_unresolved": 3,
  "fallback_requests": 1,
  "completeness": "complete",
  "error": "",
  "crawled_at": "2026-01-01T00:00:00+00:00"
}
```

**Các trạng thái `status`:**

| Trạng thái | Ý nghĩa | Resume mặc định |
|---|---|---|
| `ok` | Crawl thành công, tất cả reference đều resolved | Bỏ qua |
| `partial` | Crawl thành công nhưng một vài reference **unresolved** | Bỏ qua (trừ `--retry-partial`) |
| `not_found` | Paper chưa có trên S2 (HTTP 404) | Bỏ qua (trừ `--retry-not-found`) |
| `error` | Lỗi transport/API sau khi hết retry | Thử lại nếu `--retry-errors`, hoặc đã hết `MAX_PAPER_ATTEMPTS` |

**Metrics chi tiết trong ledger:**
- `refs_total`: tổng references API trả về cho paper này
- `refs_with_arxiv`: số reference có arXiv ID hợp lệ
- `refs_written`: số cạnh thực tế ghi vào `references.jsonl` (sau dedup)
- `raw_refs_received`: entries raw từ API trước khi lọc (đếm tất cả page)
- `refs_with_paper_id`: entries có Semantic Scholar paperId
- `refs_unresolved`: entries không xác định được identifier nào
- `fallback_requests`: số lần gọi batch lookup để bổ sung metadata thiếu
- `completeness`: `"complete"` hoặc `"partial"`

## Quy tắc dữ liệu chung

- File trung gian và đầu ra dùng JSON Lines (`.jsonl`), UTF-8, một object mỗi dòng.
- Giá trị thiếu: chuỗi → `""`, danh sách → `[]`, số → `null`.
- Mọi arXiv ID trong dữ liệu và tên file đi qua `src.utils.clean_arxiv_id()`;
  trước khi so khớp, ID được bỏ version bằng `normalize_arxiv_id()` để
  `2301.12345v3` và `2301.12345` là cùng một đỉnh.
- ID legacy 7 chữ số vẫn hợp lệ (regex `_ARXIV_ID_PATTERN` trong `src/utils.py`).
  `clean_arxiv_id()` đổi `/` thành `_`, nên `astro-ph/1234567` được lưu là
  `astro-ph_1234567` và `math/1234567` là `math_1234567`; đây là dạng chuẩn
  hoá bình thường, không phải dữ liệu lỗi.
- Khi gọi dịch vụ khác của arXiv (ví dụ tải full-text), nghỉ tối thiểu 3 giây
  giữa hai request (`utils.RateLimiter`, `ARXIV_MIN_INTERVAL`). Crawler hiện tại
  chỉ gọi Semantic Scholar.
- Client Graph API (`src/s2_api_client.py`) dùng `httpx`, header `x-api-key`,
  endpoint `/paper/arXiv:<id>`, `/paper/<id>/references` và `/paper/batch`;
  retry 429 (theo `Retry-After`) và 5xx, không retry 401/403/404.
- Client browser (`src/s2_client.py`) vẫn giữ làm fallback/dự phòng: một
  Chromium, 3 tab tái sử dụng, chung một `AsyncRateLimiter`, nghỉ 30 giây sau
  WAF challenge. Tab chỉ đóng sau khi toàn bộ hàng đợi xong (hoặc khi Ctrl-C / lỗi).
- Thống kê mỗi lần chạy có thêm `client_type`, `api_calls`, `browser_calls`,
  `rate_limited`; log có dòng `metrics` với `error_rate` và `refs_per_paper`.
- Đo hiệu năng bằng `utils.measure` / `@utils.timed` (thời gian, RAM, disk); số
  liệu chính thức đo trên Google Colab chỉ CPU.

## Đọc dữ liệu

```python
from src.read_ref import load_references

df = load_references("data/raw/member3/references.jsonl")
print(df.dtypes)
```

`load_references` trả về đủ cột trong `REQUIRED_COLUMNS`, đổi tên
`ref_publication_date` (dữ liệu cũ) thành `ref_submission_date`, và ép
`ref_authors` sang `list[str]`.

## Theo dõi tiến độ

```bash
# Log real-time
tail -f logs/crawl_references_member3.log

# Số cạnh đã ghi
wc -l data/raw/member3/references.jsonl

# Số paper đã có trạng thái trong ledger
wc -l data/raw/member3/ledger.jsonl

# Báo cáo lần chạy gần nhất
cat statistics/crawl_references_member3.md
```

## Xử lý sự cố

| Lỗi | Nguyên nhân | Khắc phục |
|---|---|---|
| `401`/`403` | Key sai, hết quyền hoặc chưa có key | Kiểm tra `S2_API_KEY`; crawler tự fallback sang browser |
| `429 Rate Limited` | Quá nhiều request | Tăng `S2_API_MIN_INTERVAL` / `S2_WEB_MIN_INTERVAL` |
| `Executable doesn't exist` | Chưa cài browser | `playwright install chromium` |
| `Target page, context or browser has been closed` | Browser bị đóng khi debug | Chạy lại; không đóng cửa sổ Chromium |
| `KeyError: memberX` | Sai `ARXIV_MEMBER` | Kiểm tra `ranges.yaml` |
| `month_sizes is missing` | Thiếu config tháng | Điền `month_sizes` hoặc dùng `--month-size` |
| Exit code `2` | Có lỗi tạm thời sau retry | Chạy lại, hoặc `--tolerate-errors N` |

(End of file - total 216 lines)