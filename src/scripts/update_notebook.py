"""Utility script to synchronize and format code cells in milestone1_pipeline.ipynb."""

import json
from pathlib import Path

def update_notebook():
    path = Path("src/milestone1_pipeline.ipynb")
    with open(path, "r", encoding="utf-8") as f:
        nb = json.load(f)

    cell7_code = """#@title Cấu hình tham số Pipeline (Khoảng ID, Limit & Tùy chọn Retry) { display-mode: "form" }

#@markdown ### 1. Khoảng arXiv ID và Giới hạn
#@markdown Nhập `start_id` và `end_id` theo định dạng `YYMM.NNNNN` (ví dụ: `2502.13235`).
start_id = "2502.13235"  #@param {type:"string"}
end_id = "2502.13245"    #@param {type:"string"}

#@markdown Giới hạn số bài báo chạy thử nghiệm (đặt `0` để chạy toàn bộ dải ID):
LIMIT = 0                #@param {type:"integer"}

#@markdown ### 2. Tùy chọn Phục hồi & Retry (Recovery Options)
#@markdown - **resume**: Bỏ qua các ID đã hoàn thành hợp lệ (khuyến nghị giữ `True`).
#@markdown - **retry_errors**: Thử lại ngay các ID từng gặp lỗi (lỗi mạng, HTTP 429/5xx).
#@markdown - **retry_not_found**: Thử lại các ID từng trả về HTTP 404 Not Found.
#@markdown - **retry_partial**: Cào bổ sung trích dẫn chưa đầy đủ (chỉ áp dụng Bước 3).
resume = True            #@param {type:"boolean"}
retry_errors = False     #@param {type:"boolean"}
retry_not_found = False  #@param {type:"boolean"}
retry_partial = False    #@param {type:"boolean"}

#@markdown ### 3. Tối ưu Hiệu năng (Performance Optimization)
#@markdown - **parallel_execution**: Chạy đồng thời Bước 2 (Tải mã nguồn) và Bước 3 (Cào trích dẫn Semantic Scholar).
#@markdown - **arxiv_download_delay**: Giãn cách tối thiểu (giây) giữa các request tải mã nguồn arXiv (mặc định 3.0s theo ToS; có thể hạ 1.5s - 2.0s khi test nhanh).
#@markdown - **batch_size_metadata**: Gom cụm truy vấn arXiv Metadata (mặc định 100 ID/request).
#@markdown - **batch_size_references**: Gom cụm truy vấn trích dẫn Semantic Scholar qua batch endpoint (mặc định 100 ID/request, tối đa 500).
parallel_execution = True  #@param {type:"boolean"}
arxiv_download_delay = 3.0  #@param {type:"number"}
batch_size_metadata = 100  #@param {type:"integer"}
batch_size_references = 100  #@param {type:"integer"}

#@markdown ### 4. Tùy chọn Ổ đĩa Cục bộ Siêu tốc (Fast Scratch NVMe Storage cho Colab)
#@markdown - **use_fast_scratch_disk**: Lưu trữ và xử lý trực tiếp trên `/content/data` (ổ NVMe siêu tốc của Colab) thay vì ghi qua Google Drive FUSE (giúp tốc độ I/O nhanh hơn tới 10x-50x và tránh hoàn toàn tình trạng nghẽn I/O hay treo do FUSE). Dữ liệu sẽ tự động đồng bộ (sync) ngược lại vào Google Drive ở cuối phiên chạy.
use_fast_scratch_disk = True  #@param {type:"boolean"}

# Dùng chung cho cả ba bước. Giữ resume=True để phục hồi lỗi, không tải lại tất cả.
RECOVERY_OPTIONS = {
    "resume": resume,
    "retry_errors": retry_errors,
    "retry_not_found": retry_not_found,
    "retry_partial": retry_partial,
}

from src.utils import parse_arxiv_range
id_list = parse_arxiv_range(start_id, end_id)
if LIMIT > 0:
    id_list = id_list[:LIMIT]

if not id_list:
    raise ValueError("id_list rỗng: vui lòng kiểm tra lại start_id và end_id")

print(f"-> Tổng số bài báo sẽ xử lý: {len(id_list)}")
print("ID đầu tiên:", id_list[0] if id_list else "None")
print("ID cuối cùng:", id_list[-1] if id_list else "None")
print("Tùy chọn phục hồi (RECOVERY_OPTIONS):", RECOVERY_OPTIONS)
print(f"Chế độ chạy song song (parallel_execution): {parallel_execution}")
print(f"Độ trễ tải mã nguồn arXiv (arxiv_download_delay): {arxiv_download_delay}s")
print(f"Kích thước batch Metadata (batch_size_metadata): {batch_size_metadata}")
print(f"Kích thước batch References (batch_size_references): {batch_size_references}")
print(f"Sử dụng Fast Scratch NVMe (use_fast_scratch_disk): {use_fast_scratch_disk}")
"""
    nb['cells'][7]['source'] = [line + '\n' for line in cell7_code.splitlines()]

    cell9_code = """import os
import shutil
from pathlib import Path
from src import config, utils
from src.benchmark import SystemBenchmarkTracker

# Kích hoạt logging ra file m1_pipeline.log
utils.setup_logging("m1_pipeline", config.LOG_DIR / "m1_pipeline.log")

# Cấu hình Fast Scratch NVMe storage trên Colab để loại bỏ độ trễ Google Drive FUSE
# Thư mục dữ liệu bền vững trên Drive (luôn dựa trên config.DEFAULT_DATA_DIR để không bị ghi đè khi chạy lại nhiều lần trong cùng phiên)
PERSISTENT_DATA_DIR = getattr(config, "DEFAULT_DATA_DIR", Path(os.environ.get("ARXIV_DATA_DIR", config.ROOT_DIR / "data")))
DRIVE_DATA_DIR = PERSISTENT_DATA_DIR

if use_fast_scratch_disk and Path("/content").exists() and DRIVE_DATA_DIR.resolve() != Path("/content/data").resolve():
    SCRATCH_DATA_DIR = Path("/content/data")
    if DRIVE_DATA_DIR.exists():
        print(f"-> Đồng bộ siêu tốc metadata & ledger từ Drive ({DRIVE_DATA_DIR}) sang ổ cục bộ ({SCRATCH_DATA_DIR})...")
        SCRATCH_DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 1. Chỉ copy thư mục CSV, JSONL và Ledger (chỉ vài KB/MB, KHÔNG copy hàng nghìn file raw TeX)
        drive_csv = DRIVE_DATA_DIR / "csv"
        scratch_csv = SCRATCH_DATA_DIR / "csv"
        if drive_csv.exists() and drive_csv.resolve() != scratch_csv.resolve():
            scratch_csv.mkdir(parents=True, exist_ok=True)
            for item in drive_csv.iterdir():
                if item.is_file():
                    target = scratch_csv / item.name
                    # Chỉ copy từ Drive sang Scratch nếu file ở Scratch chưa có hoặc file trên Drive mới hơn
                    if not target.exists() or item.stat().st_mtime > target.stat().st_mtime:
                        shutil.copy2(item, target)
        # 2. Copy các file snapshot tổng hợp (Metadata & References)
        for name in ["papers.jsonl", "papers.jsonl.journal", "references.jsonl"]:
            f = DRIVE_DATA_DIR / name
            dst = SCRATCH_DATA_DIR / name
            if f.exists() and f.resolve() != dst.resolve():
                if not dst.exists() or f.stat().st_mtime > dst.stat().st_mtime:
                    shutil.copy2(f, dst)
        print("✅ Hoàn tất đồng bộ metadata phục vụ Resume trong < 1s (bỏ qua copy đĩa raw TeX nặng)!")
    config.DATA_DIR = SCRATCH_DATA_DIR
    config.RAW_DIR = SCRATCH_DATA_DIR / "raw"
    print(f"-> Đã kích hoạt Fast Scratch Storage tại {config.DATA_DIR} (NVMe).")
else:
    config.DATA_DIR = DRIVE_DATA_DIR
    config.RAW_DIR = DRIVE_DATA_DIR / "raw"
    print(f"-> Sử dụng thư mục dữ liệu tại: {config.DATA_DIR}")

# Các thư mục lưu trữ theo đúng quy định:
# Nguồn giải nén lưu tại data/raw, thống kê CSV lưu tại data/csv
RAW_DIR = config.DATA_DIR / "raw"
CSV_DIR = config.DATA_DIR / "csv"
RAW_DIR.mkdir(parents=True, exist_ok=True)
CSV_DIR.mkdir(parents=True, exist_ok=True)
print("-> Đã thiết lập thư mục lưu trữ và logging m1_pipeline.log thành công.")
"""
    nb['cells'][9]['source'] = [line + '\n' for line in cell9_code.splitlines()]

    cell11_code = """import json
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from src.metadata_module import crawl_papers
from src.download_module.downloader import download_papers_range
from src.crawl_references_module import crawl_references_range
from src.benchmark import SystemBenchmarkTracker, calculate_storage_mb
from src.utils import clean_arxiv_id

print("-> Đang khởi động SystemBenchmarkTracker để theo dõi Bảng 4...")
# Khởi động background resource tracker (đo RAM, Disk và Wall Time)
# disk_sample_interval_sec=0.0: Tắt quét đĩa định kỳ để tránh nghẽn I/O trên Google Drive (đo dung lượng 1 lần ở cuối tại stop)
tracker = SystemBenchmarkTracker(
    storage_dirs=[config.DATA_DIR],
    sample_interval_sec=0.5,
    disk_sample_interval_sec=0.0,
).start()
print("-> Đã kích hoạt SystemBenchmarkTracker để theo dõi Bảng 4 (m1_system_benchmark.csv).")

# Snapshot tập hợp ID đã có kết quả trước khi bắt đầu phiên chạy
def snapshot_completed_ids(csv_dir, stages=None):
    if stages is None:
        stages = ["m1_discovery_stats", "m1_download_stats", "m1_reference_stats"]
    elif isinstance(stages, str):
        stages = [stages]
    seen = set()
    for base in stages:
        ledger_p = csv_dir / f"{base}.ledger.jsonl"
        jsonl_p = csv_dir / f"{base}.jsonl"
        csv_p = csv_dir / f"{base}.csv"
        # Ưu tiên đọc ledger hoặc jsonl (stream 1 lần, tránh parse redundantly cả jsonl và csv)
        if ledger_p.exists():
            try:
                with open(ledger_p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            r = json.loads(line)
                            aid = r.get("arxiv_id")
                            if aid and r.get("status") in {"ok", "pdf_only", "success"}:
                                seen.add(clean_arxiv_id(aid))
                continue
            except Exception:
                pass
        if jsonl_p.exists():
            try:
                with open(jsonl_p, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            r = json.loads(line)
                            aid = r.get("arxiv_id")
                            if aid:
                                seen.add(clean_arxiv_id(aid))
                continue
            except Exception:
                pass
        if csv_p.exists():
            try:
                df_temp = pd.read_csv(csv_p, usecols=["arxiv_id"], dtype={"arxiv_id": str})
                seen.update(df_temp["arxiv_id"].dropna().apply(clean_arxiv_id))
            except Exception:
                pass
    return seen

# Snapshot trạng thái trước khi chạy cho từng công đoạn riêng biệt
initial_download_ids = snapshot_completed_ids(CSV_DIR, stages="m1_download_stats")
initial_discovery_ids = snapshot_completed_ids(CSV_DIR, stages="m1_discovery_stats")
initial_reference_ids = snapshot_completed_ids(CSV_DIR, stages="m1_reference_stats")

# Khởi tạo lại biến kết quả trước khối try để tránh rò rỉ từ các cell chạy trước đó trong notebook
df_downloads = None
df_refs = None
stats_papers = {}
try:
    print(f"=== BẮT ĐẦU CÀO METADATA BÀI BÁO (arXiv API - Batch size: {batch_size_metadata}) ===")
    stats_papers = crawl_papers(
        id_list=id_list,
        out_path=config.DATA_DIR / "papers.jsonl",
        csv_path=CSV_DIR / "m1_discovery_stats.csv",
        batch_size=batch_size_metadata,
        **RECOVERY_OPTIONS,
    )
    print("-> Kết quả cào Metadata:", stats_papers)

    max_workers = 2 if parallel_execution else 1
    mode_name = "ĐỒNG THỜI (Parallel - 2 workers)" if parallel_execution else "TUẦN TỰ (Sequential - 1 worker)"
    print(f"\\n=== BẮT ĐẦU THỰC THI {mode_name}: TẢI SOURCE (delay: {arxiv_download_delay}s) VÀ CÀO TRÍCH DẪN (Batch S2: {batch_size_references}) ===")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_download = executor.submit(
            download_papers_range,
            id_list=id_list,
            raw_dir=RAW_DIR,
            alt_raw_dir=DRIVE_DATA_DIR / "raw" if (use_fast_scratch_disk and DRIVE_DATA_DIR.resolve() != config.DATA_DIR.resolve()) else None,
            csv_dir=CSV_DIR,
            delay=arxiv_download_delay,
            **RECOVERY_OPTIONS,
            show_progress=True,
        )
        future_refs = executor.submit(
            crawl_references_range,
            id_list=id_list,
            out_path=config.DATA_DIR / "references.jsonl",
            csv_path=CSV_DIR / "m1_reference_stats.csv",
            batch_size=batch_size_references,
            **RECOVERY_OPTIONS,
        )

        df_downloads = future_download.result()
        df_refs = future_refs.result()

    print(f"\\n-> Đã ghi thống kê tải mã nguồn cho {len(df_downloads)} ID (gồm cả lỗi).")
    print(df_downloads["download_status"].value_counts())
    print(f"\\n-> Đã ghi thống kê trích dẫn cho {len(df_refs)} ID (gồm cả lỗi).")
    print(df_refs["ref_status"].value_counts())
finally:
    print("\\n=== KẾT THÚC VÀ LƯU BENCHMARK HỆ THỐNG ===")
    # Ghi nhận chính xác số bài cào thực tế trong phiên chạy này (chống nhân dồn & đảm bảo >= 1)
    target_count = len(id_list) if id_list else 1
    target_clean = {clean_arxiv_id(x) for x in id_list}

    # 1. Xác định số bài tải mã nguồn thực tế trong phiên chạy này (session_downloads)
    session_downloads = None
    if df_downloads is not None and not df_downloads.empty:
        df_target = df_downloads[df_downloads["arxiv_id"].apply(clean_arxiv_id).isin(target_clean)]
        if not RECOVERY_OPTIONS.get("resume", True):
            # Khi resume=False, toàn bộ target_ids đều được tải lại trong phiên này
            session_downloads = df_target
        else:
            # Khi resume=True, chỉ lấy các bài mới được tải trong phiên này (chưa có trước đó)
            session_downloads = df_target[~df_target["arxiv_id"].apply(clean_arxiv_id).isin(initial_download_ids)]

    # 2. Xác định các bài mới hoàn thành qua các công đoạn (session_new_ids)
    current_download_ids = snapshot_completed_ids(CSV_DIR, stages="m1_download_stats")
    current_discovery_ids = snapshot_completed_ids(CSV_DIR, stages="m1_discovery_stats")
    current_reference_ids = snapshot_completed_ids(CSV_DIR, stages="m1_reference_stats")

    new_download = (current_download_ids - initial_download_ids) & target_clean
    new_discovery = (current_discovery_ids - initial_discovery_ids) & target_clean
    new_reference = (current_reference_ids - initial_reference_ids) & target_clean
    session_new_ids = new_download | new_discovery | new_reference

    # 3. Gán actual_papers_count theo thứ tự ưu tiên:
    # - Ưu tiên 1: Có bài tải mã nguồn mới trong phiên này (session_downloads)
    if session_downloads is not None and len(session_downloads) > 0:
        actual_papers_count = len(session_downloads)
    # - Ưu tiên 2: Có bài mới hoàn thành ở công đoạn bất kỳ (session_new_ids, ví dụ ngắt giữa chừng hoặc chỉ chạy metadata/refs)
    elif len(session_new_ids) > 0:
        actual_papers_count = len(session_new_ids)
    # - Ưu tiên 3: Dự phòng khi toàn bộ bài đã hoàn thành từ trước (không có bài mới), gán bằng target_count (len(id_list))
    else:
        actual_papers_count = target_count

    # Chốt chặn an toàn: đảm bảo tuyệt đối không bao giờ bằng 0
    actual_papers_count = max(1, int(actual_papers_count))

    # Tính dung lượng thực tế của toàn bộ thư mục dữ liệu (raw, jsonl, csv)
    final_storage_mb = calculate_storage_mb([config.DATA_DIR])

    print(f"-> Số bài cào thực tế trong phiên này: {actual_papers_count}")
    print(f"-> Dung lượng thư mục dữ liệu ({config.DATA_DIR}): {final_storage_mb} MB")
    df_benchmark = tracker.stop(
        total_papers=actual_papers_count,
        csv_path=CSV_DIR / "m1_system_benchmark.csv",
        final_storage_mb=final_storage_mb,
    )
    print("-> Bảng 4: m1_system_benchmark:")
    display(df_benchmark)

    # Tự động đồng bộ dữ liệu từ Fast Scratch sang Google Drive nếu được kích hoạt
    if use_fast_scratch_disk and Path("/content").exists() and DRIVE_DATA_DIR.resolve() != config.DATA_DIR.resolve():
        print(f"\\n-> Đang đồng bộ siêu tốc kết quả từ ổ cục bộ ({config.DATA_DIR}) vào Google Drive ({DRIVE_DATA_DIR})...")
        try:
            # 1. Đồng bộ thư mục CSV, JSONL và Ledger
            drive_csv = DRIVE_DATA_DIR / "csv"
            scratch_csv = config.DATA_DIR / "csv"
            if scratch_csv.exists():
                drive_csv.mkdir(parents=True, exist_ok=True)
                for item in scratch_csv.iterdir():
                    if item.is_file():
                        shutil.copy2(item, drive_csv / item.name)

            # 2. Đồng bộ các file snapshot tổng hợp (Metadata & References)
            for name in ["papers.jsonl", "papers.jsonl.journal", "references.jsonl"]:
                f = config.DATA_DIR / name
                if f.exists():
                    shutil.copy2(f, DRIVE_DATA_DIR / name)

            # 3. Đồng bộ raw source folders chỉ cho các ID trong phiên này
            scratch_raw = config.DATA_DIR / "raw"
            drive_raw = DRIVE_DATA_DIR / "raw"
            if scratch_raw.exists():
                drive_raw.mkdir(parents=True, exist_ok=True)
                for aid in id_list:
                    c_aid = clean_arxiv_id(aid)
                    s_paper = scratch_raw / c_aid
                    d_paper = drive_raw / c_aid
                    if s_paper.exists():
                        shutil.copytree(s_paper, d_paper, dirs_exist_ok=True)
            print("✅ Đồng bộ siêu tốc hoàn tất! Dữ liệu đã được lưu an toàn trên Google Drive.")
        except Exception as sync_err:
            print(f"⚠️ Cảnh báo lỗi đồng bộ Google Drive: {sync_err}")
"""
    nb['cells'][11]['source'] = [line + '\n' for line in cell11_code.splitlines()]

    output_path = Path("/tmp/milestone1_pipeline_updated.ipynb")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
    print("Saved updated notebook to", output_path)

if __name__ == "__main__":
    update_notebook()
