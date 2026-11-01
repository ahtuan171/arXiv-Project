# Milestone 1 — One repository, seven code cells

Nhóm giữ **một repository duy nhất**: code trong src/, notebooks/ debug, Jupyter Book trong book/ và CI trong .github/. Dữ liệu lớn lưu trên Google Drive, không commit.

- Main pipeline: src/milestone1_pipeline.ipynb
- Read-only legacy/debug: notebooks/m1_legacy_debug.ipynb
- Jupyter Book: book/ trong cùng repo
- Full data contract: docs/m1-data-contract.md

## Thứ tự chạy trên Google Colab

| Cell code | Công việc | Ghi dữ liệu |
|---|---|---|
| 1 Setup | Mount Drive; clone/pull code vào /content; pip install | Không thay đổi dataset |
| 2 Config | ID range, Secrets, tất cả cờ ghi/tải | Không |
| 3 Preflight/Test | Metadata-only audit và pytest offline | Không |
| 4 Source Versions | Cào từng vN, retry và resume | Chỉ khi DOWNLOAD_VERSIONS=True |
| 5 Raw References | S2 raw citations (kể cả non-arXiv) | Chỉ khi FETCH_RAW_REFERENCES=True |
| 6 Validation | Một lượt hash SHA-256 + metadata + integrity | Chỉ khi bật export flags |
| 7 Summary | Coverage, citations và thiếu bằng chứng benchmark | Chỉ khi EXPORT_M1_REPORT=True |

Mặc định các cờ network/export bằng False. Chạy code cells 1 đến 7 để test an toàn. Để thử tải ít dữ liệu, bật cờ trong cell 2, giữ VERSION_LIMIT=3 và RAW_REF_LIMIT=3, rồi chạy các cell 4 đến 7. Sau mọi lần tải tiếp, **chạy lại cell 6 rồi cell 7**. Không chạy hai notebook ghi vào cùng thư mục Drive đồng thời.

Không chạy lại cell clone Git cũ trên Drive. Legacy crawler cũ không còn trong notebook chính nhưng code và lịch sử Git vẫn được giữ. Notebook debug chỉ đọc dữ liệu cũ, không tự di chuyển hoặc gán lịch sử phiên bản.

## Benchmark

CSV benchmark legacy không được coi là kết quả đo mới. Benchmark cuối cùng của pipeline vN phải được đo trên Colab CPU-only với thông tin phạm vi, wall time, peak/mean RAM, disk và storage. Jupyter Book nằm tại book/ trong cùng repo; nhóm tự đối chiếu điều kiện nộp bài với giảng viên.

## Test

Workflow Milestone 1 offline regression chạy pytest bằng dữ liệu giả. Nó không truy cập Drive; thành công trên CI không đảm bảo dataset thực tế đã tải xong.
