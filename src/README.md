# Milestone 1 — Colab notebook theo luồng xử lý

Notebook chính: `src/milestone1_pipeline.ipynb`. Code ở `/content/arxiv-code`; dữ liệu ở Google Drive. Notebook debug legacy nằm tại `notebooks/m1_legacy_debug.ipynb`.

| Cell | Bước | Ghi dữ liệu |
|---|---|---|
| 1 | Kết nối Drive, lấy code | Không |
| 2 | Cài requirements | Chỉ môi trường Colab |
| 3 | Đọc S2 API key từ Secrets | Không |
| 4 | Chọn dải ID và tùy chọn | Không |
| 5 | Bắt đầu benchmark tùy chọn | Không |
| 6 | Crawl metadata | `papers.jsonl`, Bảng 1; mặc định bật |
| 7 | Kiểm tra metadata, test offline tùy chọn | Không |
| 8 | Tải source từng vN | Chỉ khi `DOWNLOAD_VERSIONS=True` |
| 9 | Thu raw references | Chỉ khi `FETCH_RAW_REFERENCES=True` |
| 10 | Kiểm định toàn dataset | Chỉ xuất khi bật cờ export |
| 11 | Kết thúc benchmark | CSV mới khi `RUN_BENCHMARK=True` |
| 12 | Tổng kết và hiển thị bảng | Chỉ xuất khi `EXPORT_M1_REPORT=True` |

Lần chạy đầu hãy chọn dải nhỏ bằng `LIMIT`. Metadata mặc định được crawl với resume; source và references mặc định chỉ lập kế hoạch. Khi chạy lại sau lỗi, thực thi cell thu thập tương ứng rồi chạy lại cell 10 và 12. Không chạy hai notebook cùng ghi vào một thư mục Drive.

Benchmark mới lưu tại `csv/m1_version_pipeline_benchmark.csv`, tách khỏi Bảng 4 legacy. Chỉ báo cáo số liệu chính thức sau lượt chạy đủ phạm vi trên Colab CPU-only; các lần chạy thử chỉ là số liệu của lượt thử.

Bộ test offline không truy cập Drive hay API. Kết quả test không chứng minh dataset thật đã hoàn thành.
