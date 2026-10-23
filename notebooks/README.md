# 📓 Hướng Dẫn Benchmark Module Cào References trên Google Colab

Thư mục này chứa mã nguồn và notebook Jupyter chuyên dụng để **kiểm tra benchmark hiệu suất** của module cào references arXiv từ Semantic Scholar Graph API trên môi trường **Google Colab (CPU-Only)**.

---

## 📂 Danh Sách Tệp

| Tệp | Mô Tả |
|---|---|
| [`benchmark_crawl_references.ipynb`](file:///home/cheems/Projects/arXiv-Project/notebooks/benchmark_crawl_references.ipynb) | Jupyter Notebook chính chạy trực tiếp trên Google Colab với giao diện trực quan, biểu đồ và xuất báo cáo tự động |
| [`benchmark_runner.py`](file:///home/cheems/Projects/arXiv-Project/notebooks/benchmark_runner.py) | Module Python độc lập chứa toàn bộ hàm đo lường, profile phần cứng, stress test và tạo biểu đồ |
| [`build_notebook.py`](file:///home/cheems/Projects/arXiv-Project/notebooks/build_notebook.py) | Script sinh tự động tệp `.ipynb` chuẩn |

---

## 🚀 Cách Chạy Trên Google Colab

### Cách 1: Mở Trực Tiếp Trên Google Colab
1. Truy cập [Google Colab](https://colab.research.google.com/).
2. Chọn tab **GitHub** hoặc **Upload** tệp [`benchmark_crawl_references.ipynb`](file:///home/cheems/Projects/arXiv-Project/notebooks/benchmark_crawl_references.ipynb).
3. Tại menu Colab, chọn **Runtime** -> **Change runtime type** -> Đảm bảo đã chọn **CPU** (mặc định của Colab là 2 vCPUs).
4. Thực thi từng cell theo thứ tự.

### Cách 2: Chạy Qua Dòng Lệnh (Colab Terminal hoặc Local)
```bash
# 1. Cài đặt thư viện
pip install -r requirements.txt psutil

# 2. Chạy benchmark đầy đủ (Live API + Concurrency + CPU Stress Test)
python notebooks/benchmark_runner.py --samples 10 --workers 1 2 3

# Hoặc chỉ chạy stress test CPU (không tốn quota mạng):
python notebooks/benchmark_runner.py --skip-live --stress-refs 10000
```

---

## 📊 Các Bài Kiểm Tra Benchmark Được Thực Hiện

1. **Kiểm tra thông số phần cứng (Hardware Profiling):**
   - Model CPU (`Intel Xeon` hoặc `AMD EPYC`), số lượng vCPU, xung nhịp, dung lượng RAM khả dụng.
2. **Benchmark Live Semantic Scholar Graph API:**
   - Đo Throughput: số `papers/giây`, `refs/giây`, `edges/giây`.
   - Phân bố độ trễ (Latency distribution): `Min`, `Mean`, `Median (P50)`, `P90`, `P95`, `Max`, `StdDev`.
   - Phễu phân giải dữ liệu (Resolution Funnel):
     $$\text{Raw API Refs} \longrightarrow \text{Có S2 paperId} \longrightarrow \text{Có arXiv ID} \longrightarrow \text{Cạnh hợp lệ đã ghi}$$
   - Theo dõi lỗi (HTTP 429 Rate limit, 404 Not Found, 5xx Network Errors).
3. **Khảo sát mở rộng Concurrency (Worker Scaling):**
   - So sánh giữa `1, 2, 3, 4 workers` trên môi trường 2 vCPUs.
   - Đo lường mức tiêu thụ CPU (%) và RAM RSS (MB) theo số lượng worker.
4. **Kiểm tra áp lực thuần CPU (CPU-Bound Stress Test):**
   - Giả lập hàng nghìn references để đo tốc độ tối đa của CPU Colab khi xử lý parse JSON, chuẩn hóa regex arXiv ID, deduplicate qua hash set và ghi JSONL ra đĩa.
5. **Benchmark Data Loader (`load_references`):**
   - Đo tốc độ đọc tệp `references.jsonl` vào `pandas.DataFrame` và mức chiếm dụng RAM của DataFrame.

---

## 📈 Kết Quả Đầu Ra

Sau khi hoàn thành, hệ thống sẽ tự động xuất:
- **`statistics/benchmark_colab_cpu.json`**: Dữ liệu thô và toàn bộ chỉ số định lượng.
- **`statistics/benchmark_colab_cpu.md`**: Báo cáo Markdown chi tiết theo tiêu chuẩn khoa học.
- **`statistics/benchmark_colab_cpu_charts.png`**: Dashboard đồ họa 4 panels gồm:
  1. Phân bố độ trễ (Latency Distribution).
  2. Phễu chuyển đổi dữ liệu references (Funnel Conversion).
  3. Đường cong Concurrency Scaling (Workers vs Throughput & CPU %).
  4. Lược đồ tiêu thụ CPU & RAM theo thời gian.
