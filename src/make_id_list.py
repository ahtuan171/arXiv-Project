# src/make_id_list.py
"""Sinh data/id_list.txt cho MỘT phần (part) của nhóm.

Cách dùng (chạy từ gốc repo):
    python -m src.make_id_list --part 1      # phần của thành viên 1
    python -m src.make_id_list --part all    # kiểm tra cả 25.000 ID
"""
import argparse
import os

# Mỗi phần = (start_month, start_id, end_month, end_id), chép đúng từ sheet
PARTS = {
    1: ("2025-02", 13235, "2025-02", 18234),
    2: ("2025-01", 7642,  "2025-01", 12641),
    3: ("2024-12", 3848,  "2024-12", 8847),
    4: ("2024-12", 13848, "2024-12", 18847),
    5: ("2025-01", 17642, "2025-02", 3234),     # vắt qua 2 tháng
}

# Số ID lớn nhất của tháng, CHỈ cần cho dải vắt qua 2 tháng (phần 4).
# Suy ra từ: (MAX - 17642 + 1) + 3234 = 5000. Cần kiểm tra trên arxiv.org.
MONTH_MAX = {
    "2025-01": 19407,
}

def yymm(month: str) -> str:
    y, m = month.split("-")
    return y[2:] + m                      # "2025-02" -> "2502"

def ids_in_range(sm, si, em, ei):
    if sm == em:
        return [f"{yymm(sm)}.{n:05d}" for n in range(si, ei + 1)]
    first = [f"{yymm(sm)}.{n:05d}" for n in range(si, MONTH_MAX[sm] + 1)]
    second = [f"{yymm(em)}.{n:05d}" for n in range(1, ei + 1)]
    return first + second

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", required=True, help="1..5 hoặc all")
    ap.add_argument("--out", default="data/id_list.txt")
    args = ap.parse_args()

    keys = list(PARTS) if args.part == "all" else [int(args.part)]
    ids = []
    for k in keys:
        part_ids = ids_in_range(*PARTS[k])
        assert len(part_ids) == 5000, f"phần {k} có {len(part_ids)} ID, phải là 5000"
        ids += part_ids
    assert len(ids) == len(set(ids)), "có ID bị trùng"

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(ids) + "\n")
    print(f"Đã ghi {len(ids)} ID vào {args.out}")
    print("Đầu:", ids[:2], "| Cuối:", ids[-2:])

if __name__ == "__main__":
    main()