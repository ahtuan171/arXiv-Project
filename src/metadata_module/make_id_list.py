"""CLI utility to generate arXiv ID list from start_id and end_id.

Usage:
    python -m src.metadata_module.make_id_list --start-id 2502.13235 --end-id 2502.18234 --out data/id_list.txt
    python -m src.metadata_module.make_id_list --start-id 13235 --end-id 18234 --month 2502 --out data/id_list.txt
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Union

from src.utils import parse_arxiv_range


def generate_id_list(
    start_id: Union[str, int],
    end_id: Union[str, int],
    month: Optional[str] = None,
    out_path: Optional[Union[Path, str]] = None,
) -> list[str]:
    """Generate arXiv IDs within range [start_id, end_id]."""
    ids = parse_arxiv_range(start_id, end_id, month=month)

    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(ids) + "\n", encoding="utf-8")

    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description="Sinh danh sach arXiv ID tu khoang start_id den end_id")
    ap.add_argument("--start-id", required=True, help="ID bat dau (e.g. 2502.13235 hoac 13235)")
    ap.add_argument("--end-id", required=True, help="ID ket thuc (e.g. 2502.18234 hoac 18234)")
    ap.add_argument("--month", default=None, help="Thang (e.g. 2502), can thiet neu truyen integer ID")
    ap.add_argument("--out", default="data/id_list.txt", help="File output (mac dinh data/id_list.txt)")
    args = ap.parse_args()

    ids = generate_id_list(args.start_id, args.end_id, month=args.month, out_path=args.out)
    print(f"Da ghi {len(ids)} ID vao {args.out}")
    if ids:
        print("Dau:", ids[:2], "| Cuoi:", ids[-2:])


if __name__ == "__main__":
    main()
