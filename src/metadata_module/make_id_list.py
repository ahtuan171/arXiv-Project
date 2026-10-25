# src/metadata_module/make_id_list.py
"""Sinh danh sach arXiv ID cho cac phan (part) cua nhom.

Cach dung (chay tu goc repo):
    python -m src.metadata_module.make_id_list --part 1      # phan cua thanh vien 1
    python -m src.metadata_module.make_id_list --part all    # kiem tra ca 25.000 ID
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional, Union

# Moi phan = (start_month, start_id, end_month, end_id), chep dung tu sheet dang ky
PARTS: dict[int, tuple[str, int, str, int]] = {
    1: ("2025-02", 13235, "2025-02", 18234),
    2: ("2025-01", 7642,  "2025-01", 12641),
    3: ("2024-12", 3848,  "2024-12", 8847),
    4: ("2024-12", 13848, "2024-12", 18847),
    5: ("2025-01", 17642, "2025-02", 3234),     # vat qua 2 thang
}

# So ID lon nhat cua thang, CHI can cho dai vat qua 2 thang (phan 5).
# Suy ra tu: (MAX - 17642 + 1) + 3234 = 5000.
MONTH_MAX: dict[str, int] = {
    "2025-01": 19407,
}


def yymm(month: str) -> str:
    """Chuyen dang '2025-02' thanh '2502'."""
    y, m = month.split("-")
    return y[2:] + m


def ids_in_range(sm: str, si: int, em: str, ei: int) -> list[str]:
    """Sinh danh sach arXiv ID trong khoang [sm.si, em.ei]."""
    if sm == em:
        return [f"{yymm(sm)}.{n:05d}" for n in range(si, ei + 1)]
    first = [f"{yymm(sm)}.{n:05d}" for n in range(si, MONTH_MAX[sm] + 1)]
    second = [f"{yymm(em)}.{n:05d}" for n in range(1, ei + 1)]
    return first + second


def generate_id_list(
    part: Union[int, str],
    out_path: Optional[Union[Path, str]] = None,
) -> list[str]:
    """Sinh danh sach ID cho 1 phan hoac tat ca cac phan.

    Args:
        part: 1..5 hoac "all"
        out_path: Duong dan file de luu (tuy chon)

    Returns:
        Danh sach arXiv ID
    """
    keys = list(PARTS) if str(part).lower() == "all" else [int(part)]
    ids: list[str] = []
    for k in keys:
        if k not in PARTS:
            raise ValueError(f"Khong ton tai phan {k}. Cac phan hop le: {list(PARTS.keys())}")
        part_ids = ids_in_range(*PARTS[k])
        assert len(part_ids) == 5000, f"Phan {k} co {len(part_ids)} ID, yeu cau 5000"
        ids.extend(part_ids)

    assert len(ids) == len(set(ids)), "Phat hien ID bi trung lap"

    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(ids) + "\n", encoding="utf-8")

    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description="Sinh danh sach arXiv ID")
    ap.add_argument("--part", required=True, help="1..5 hoac all")
    ap.add_argument("--out", default="data/id_list.txt", help="File output (mac dinh data/id_list.txt)")
    args = ap.parse_args()

    ids = generate_id_list(args.part, out_path=args.out)
    print(f"Da ghi {len(ids)} ID vao {args.out}")
    print("Dau:", ids[:2], "| Cuoi:", ids[-2:])


if __name__ == "__main__":
    main()
