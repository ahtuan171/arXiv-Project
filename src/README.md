# Hướng Dẫn Chạy Pipeline Milestone 1 Trên Google Colab

Tài liệu này hướng dẫn chi tiết quy trình chạy notebook [`milestone1_pipeline.ipynb`](milestone1_pipeline.ipynb) trên môi trường Google Colab. Pipeline này tích hợp toàn diện 4 giai đoạn của Milestone 1 theo đúng đặc tả dự án.

---

## 1. Tổng quan Pipeline Milestone 1

Notebook [`milestone1_pipeline.ipynb`](milestone1_pipeline.ipynb) tự động hóa toàn bộ luồng xử lý:

| Giai đoạn | Nhiệm vụ chính | Dữ liệu lưu trữ | Bảng thống kê đầu ra (`data/csv/`) |
| :--- | :--- | :--- | :--- |
| **Giai đoạn 1** | Cào Metadata từ arXiv API | `data/papers.jsonl` | **Bảng 1:** `m1_discovery_stats.csv` |
| **Giai đoạn 2** | Tải mã nguồn & giải nén **chỉ giữ `.tex` và `.bib`** | `data/raw/<clean_arxiv_id>/` | **Bảng 2:** `m1_download_stats.csv` |
| **Giai đoạn 3** | Cào trích dẫn (References) từ Semantic Scholar | `data/references.jsonl` | **Bảng 3:** `m1_reference_stats.csv` |
| **Giai đoạn 4** | Giám sát tài nguyên hệ thống (RAM, Disk, Wall Time) | — | **Bảng 4:** `m1_system_benchmark.csv` |

### Các đặc điểm kỹ thuật nổi bật:
- **Lọc sạch mã nguồn:** Chỉ giữ lại duy nhất các file `.tex` và `.bib`. Toàn bộ file ảnh (`.png`, `.jpg`, `.pdf`, v.v.) và các file nhị phân/phụ trợ khác đều bị loại bỏ ngay khi giải nén để tối ưu dung lượng đĩa.
- **Cơ chế Retry & Rate-limit an toàn:** Tự động bắt và backoff với lỗi HTTP 429 (Too Many Requests), HTTP 500, 502, 503, 504 và mất kết nối mạng đột ngột. Tuân thủ giãn cách $\ge 3.0$s với arXiv và $\ge 1.0$s với Semantic Scholar.
- **Hỗ trợ Resume (Lưu vết tiến độ):** Khi bị gián đoạn runtime hoặc dừng giữa chừng, lần chạy tiếp theo tự động phát hiện và bỏ qua các bài đã hoàn thành.

---

## 2. Chuẩn bị Trước khi Chạy

1. **Tài khoản Google Drive:** Đảm bảo Google Drive còn trống tối thiểu 5 – 10 GB nếu chạy trọn gói 5.000 bài báo.
2. **Semantic Scholar API Key (Khuyến nghị):**
   - Đăng ký miễn phí tại [Semantic Scholar API](https://www.semanticscholar.org/product/api).
   - Khi có API Key, bạn gửi được tới 10 req/s (thay vì 1 req/s ở chế độ public) và tránh tối đa lỗi 429.

---

## 3. Các Bước Thực Hiện Chi Tiết Trên Google Colab

### Bước 1: Mở Notebook trên Google Colab
- Truy cập [Google Colab](https://colab.research.google.com/).
- Chọn tab **Upload** và tải file [`src/milestone1_pipeline.ipynb`](milestone1_pipeline.ipynb) lên, hoặc mở trực tiếp từ GitHub repository.

### Bước 2: Kết nối Google Drive & Thiết lập Môi trường (Cell 1 & 2)
Chạy cell kết nối Google Drive:
```python
from google.colab import drive
import os

drive.mount('/content/drive')

# Đường dẫn lưu trữ dự án trên Google Drive của bạn
PROJECT_DIR = '/content/drive/MyDrive/Colab_Notebooks/arXiv-Project'

if not os.path.exists(PROJECT_DIR):
    !git clone -b cv1-metadata https://github.com/ahtuan171/arXiv-Project.git "{PROJECT_DIR}"
else:
    !git -C "{PROJECT_DIR}" pull origin cv1-metadata

os.chdir(PROJECT_DIR)
print("Thư mục làm việc hiện tại:", os.getcwd())
```

Tiếp tục chạy cell cài đặt thư viện phụ thuộc:
```python
%pip install -r requirements.txt
```

> [!TIP]
> **Cấu hình API Key trên Colab:**
> Bạn có thể tạo file `.env` ngay trong thư mục `PROJECT_DIR` trên Drive với nội dung:
> ```env
> S2_API_KEY=your_actual_api_key_here
> ```
> Hoặc gán trực tiếp qua Secrets / Environment Variables của Colab:
> ```python
> import os
> os.environ["S2_API_KEY"] = "your_actual_api_key_here"
> ```

---

### Bước 3: Cấu hình Khoảng arXiv ID (Cell 3)
Thiết lập khoảng ID cần cào dữ liệu qua 2 biến `start_id` và `end_id`:

```python
start_id = "2502.13235"
end_id = "2502.14234"   # Ví dụ cào 1.000 bài

# Chạy thử nghiệm: gán LIMIT = 5 để kiểm tra tính thông suốt, gán 0 để chạy toàn bộ dải ID
LIMIT = 0

from src.utils import parse_arxiv_range
id_list = parse_arxiv_range(start_id, end_id)
if LIMIT > 0:
    id_list = id_list[:LIMIT]

print(f"-> Tổng số bài báo sẽ xử lý: {len(id_list)}")
```

> [!IMPORTANT]
> **Chiến lược chạy 5.000 bài (Khuyến nghị chia 5 đợt x 1.000 bài):**
> Colab Free thường giới hạn runtime liên tục (khoảng 6–12 tiếng). Để an toàn và ổn định nhất:
> - **Đợt 1:** `start_id = "2502.13235"`, `end_id = "2502.14234"` (1.000 bài)
> - **Đợt 2:** `start_id = "2502.14235"`, `end_id = "2502.15234"` (1.000 bài)
> - **Đợt 3:** `start_id = "2502.15235"`, `end_id = "2502.16234"` (1.000 bài)
> - **Đợt 4:** `start_id = "2502.16235"`, `end_id = "2502.17234"` (1.000 bài)
> - **Đợt 5:** `start_id = "2502.17235"`, `end_id = "2502.18234"` (1.000 bài)
> 
> Nhờ `resume=True`, các ID hoàn tất được bỏ qua sau khi kiểm tra dữ liệu. JSONL lưu lịch sử/tiến độ; CSV được xuất lại theo kết quả mới nhất của từng ID, không cộng thêm dòng trùng.

---

### Bước 4: Khởi động Benchmark & Chạy 3 Giai đoạn Thu thập (Cell 4 đến 7)

Chạy tuần tự các cell tiếp theo:
1. **Khởi động Benchmark Tracker (Cell 4):** Bắt đầu tiến trình nền định kỳ lấy mẫu RAM, theo dõi Disk và tính thời gian tổng (Wall time).
2. **Cào Metadata arXiv (Cell 5):** Gọi `crawl_papers`, ghi thông tin vào `data/papers.jsonl` và xuất **Bảng 1** (`m1_discovery_stats.csv`).
3. **Tải & Lọc Nguồn TeX (Cell 6):** Gọi `download_papers_range`, tải file `.tar.gz`, giải nén và lọc **chỉ giữ lại `.tex` và `.bib`**, lưu vào `data/raw/<clean_arxiv_id>/`, xuất **Bảng 2** (`m1_download_stats.csv`).
4. **Cào Trích dẫn Semantic Scholar (Cell 7):** Gọi `crawl_references_range`, truy vấn Semantic Scholar Graph API, lưu vào `data/references.jsonl`, xuất **Bảng 3** (`m1_reference_stats.csv`).
5. **Dừng Benchmark và Xuất Bảng 4 (Cell 8):** Ghi thông số hiệu năng vào **Bảng 4** (`m1_system_benchmark.csv`).

---

### Bước 5: Xem và Kiểm tra 4 Bảng Thống kê (Cell 9)
Cell cuối cùng sẽ tự động nạp và hiển thị trực quan các bảng kết quả:
- **Bảng 1:** `arxiv_id`, `discovery_status`, `num_versions`, `discovery_time_sec`.
- **Bảng 2:** `arxiv_id`, `download_status`, `has_tex`, `raw_size_bytes`, `cleaned_size_bytes`, `num_tex_files`, `download_time_sec`.
- **Bảng 3:** `arxiv_id`, `ref_status`, `num_crawled_refs`, `num_arxiv_refs`, `ref_time_sec`.
- **Bảng 4:** `total_papers`, `total_wall_time_sec`, `peak_ram_mb`, `average_ram_mb`, `peak_disk_mb`, `final_storage_mb`.

---

## 4. Xử Lý Khi Gặp Sự Cố & Câu Hỏi Thường Gặp

### Q1: Colab bị mất kết nối (Disconnected) giữa chừng thì làm sao?
> **Trả lời:** Chỉ cần kết nối lại Colab, chạy lại từ Cell 1 đến Cell 8. Tất cả các hàm (`crawl_papers`, `download_papers_range`, `crawl_references_range`) đều đã bật mặc định `resume=True`, hệ thống sẽ tự động quét các bài đã hoàn thành và tiếp tục xử lý các bài còn dang dở.

### Q2: Nếu chia thành 5 đợt chạy (mỗi đợt 1.000 bài), tính các chỉ số Bảng 4 Benchmark như thế nào?
> **Trả lời:** Mỗi đợt chạy sẽ ghi thêm 1 dòng vào `m1_system_benchmark.csv`. Khi tổng kết toàn bộ 5.000 bài:
> - `total_papers`: Tổng số bài của 5 đợt ($1000 \times 5 = 5000$).
> - `total_wall_time_sec`: Tổng thời gian chạy thực tế ($\sum \text{wall\_time}_i$).
> - `peak_ram_mb`: Giá trị RAM cao nhất trong cả 5 đợt ($\max(\text{peak\_ram}_i)$).
> - `average_ram_mb`: Trung bình cộng RAM giữa các đợt ($\frac{1}{5} \sum \text{average\_ram}_i$).
> - `peak_disk_mb`: Dung lượng đĩa tối đa đạt được ở đợt chạy cuối cùng.
> - `final_storage_mb`: Dung lượng lưu trữ thư mục `data/` ghi nhận ở đợt cuối cùng sau khi đã cào xong toàn bộ 5.000 bài.


### Q3: Các ID từng tải lỗi có được thử lại không?
> **Trả lời:** Có. Cả `crawl_papers`, `download_papers_range` và `crawl_references_range` dùng cùng chính sách:
>
> - Mỗi request lỗi tạm thời có tối đa **5 lần gọi tổng cộng** (lần đầu + 4 lần thử lại). Hết lượt thì ghi `error`, chuyển sang ID tiếp theo.
> - Chạy lại với `resume=True` sẽ thử những lỗi đã đến hạn. Thời gian chờ giữa các lượt là 60, 120, 240… giây, tối đa 1 giờ; bộ đếm reset khi thành công.
> - Việc thử lại xảy ra khi bạn chạy hàm/cell lần nữa, không chạy nền. Lỗi cũ chưa có lịch chờ được xét lại ngay một lần để phân loại.

Notebook có `RECOVERY_OPTIONS` ở cell cấu hình, truyền chung vào ba hàm:

```python
RECOVERY_OPTIONS = {
    "resume": True,
    "retry_errors": False,
    "retry_not_found": False,
    "retry_partial": False,
}
```

Muốn thử lại lỗi ngay sau khi mạng ổn định hoặc đã sửa API key/quyền:

```python
RECOVERY_OPTIONS["retry_errors"] = True
# Chạy lại các cell Metadata, Source và References; kết quả thành công vẫn được bỏ qua.
# Sau đợt phục hồi, đặt lại False để áp dụng thời gian chờ bình thường.
```

| Trạng thái trong ledger | Ý nghĩa | Xử lý khi chạy lại |
| :--- | :--- | :--- |
| `ok` | Đã thu thập thành công | Bỏ qua nếu dữ liệu đầu ra còn hợp lệ |
| `error` | Mạng/429/5xx hết retry hoặc gói source hỏng | Thử lại sau thời gian chờ; `retry_errors=True` để thử ngay |
| `blocked` | Quyền/key/request không hợp lệ, không nên lặp lại nguyên trạng | Sửa nguyên nhân rồi dùng `retry_errors=True` |
| `not_found` | HTTP 404 hoặc dịch vụ không tìm thấy paper | Chỉ thử lại với `retry_not_found=True` |
| `pdf_only` | arXiv trả PDF, không có source TeX | Bỏ qua; đây là kết quả kiểm tra hợp lệ |
| `partial` | References trả kết quả nhưng còn unresolved | Chỉ bổ sung với `retry_partial=True` |

`retry_partial` được cả ba hàm chấp nhận để dùng chung cấu hình; chỉ bước references có trạng thái này. `resume=False` yêu cầu xử lý lại cả dải, thường không cần khi phục hồi lỗi.

### Q4: Xem lỗi ở đâu và dữ liệu cũ được phục hồi như thế nào?

Ba file ledger nằm cạnh thống kê:

- `data/csv/m1_discovery_stats.ledger.jsonl` — metadata.
- `data/csv/m1_download_stats.ledger.jsonl` — source.
- `data/csv/m1_reference_stats.ledger.jsonl` — references trong pipeline.

Mỗi dòng ghi một lần cập nhật. Phải lấy **dòng mới nhất theo ID**, không đếm số dòng để suy ra số bài hoàn tất:

```python
from src.recovery import latest_rows

ledger = latest_rows(CSV_DIR / "m1_download_stats.ledger.jsonl")
for arxiv_id, row in ledger.items():
    if row["status"] in {"error", "blocked"}:
        print(arxiv_id, row.get("error_type"), row.get("error"), row.get("next_retry_at"))
```

`attempts` là số lượt xử lý thất bại liên tiếp của paper, không phải số request bên trong một lượt. `next_retry_at` là Unix timestamp; chỉ dùng cho `error`. Schema CSV Bảng 1–3 giữ nguyên; `failed` vẫn là thất bại, `pdf_only` là kết quả riêng của source. References `partial` vẫn ghi `success` trong Bảng 3 vì truy vấn đã trả dữ liệu; xem ledger để biết mức đầy đủ.

Dữ liệu tạo bằng code cũ được đọc tự động: metadata chứa `fetch_failed` và download/references `failed` được đưa về diện thử lại; `not_found`/`pdf_only` giữ chính sách trên. Source từng báo thành công nhưng mất file TeX hoặc chỉ có file rỗng sẽ được tải lại. Không cần xóa file cũ để bắt đầu phục hồi.

Metadata ghi journal `papers.jsonl.journal` trước khi cập nhật ledger, rồi hợp nhất thành `papers.jsonl` khi kết thúc hoặc Ctrl-C. Nếu runtime bị tắt hẳn, lần chạy sau và API đọc metadata sẽ đọc cả journal. Source giải nén vào thư mục tạm `.<ID>.staging-*`, giữ backup `.<ID>.backup` khi thay thư mục và tự phục hồi ở lần chạy sau. References chống trùng theo cặp ID; các cạnh trùng từ dữ liệu cũ được hợp nhất khi chạy lại.

Chỉ chạy **một lượt cho cùng bộ file đầu ra** tại một thời điểm. Source có khóa theo paper trên hệ thống hỗ trợ `fcntl`; crawler CLI có khóa theo member. Trên nền tảng không hỗ trợ khóa này, tránh chạy song song vào cùng thư mục. Không xóa journal/backup khi pipeline đang chạy. Lỗi ổ đĩa hoặc quyền ghi sẽ được báo ra thay vì bị ghi nhầm là thành công.
