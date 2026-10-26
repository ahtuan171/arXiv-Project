"""Project-wide configuration: paths, rate limits and per-member arXiv ID ranges.

Every member crawls their own arXiv ID range (see ``ranges.yaml``). Select the
range with the ``ARXIV_MEMBER`` environment variable (e.g. ``member3``) or by
passing the key to :func:`load_member_range`.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional

import yaml

# --- Paths -----------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file if present
_env_file = ROOT_DIR / ".env"
if _env_file.is_file():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        with open(_env_file, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip().strip("'\"")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v

DATA_DIR = Path(os.environ.get("ARXIV_DATA_DIR", ROOT_DIR / "data"))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
LOG_DIR = ROOT_DIR / "logs"
RANGES_FILE = ROOT_DIR / "ranges.yaml"
ENV_FILE = ROOT_DIR / ".env"
SCHEMA_VERSION = 2


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load simple ``KEY=VALUE`` pairs from ``.env`` without overriding the shell.

    The project only needs a tiny subset of dotenv syntax, so this avoids an
    extra dependency. Existing environment variables always win.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_env_file()

# --- Network politeness ----------------------------------------------------
# arXiv requires at least 3 seconds between requests.
ARXIV_MIN_INTERVAL = 3.2
# The website JSON endpoints tolerate a few requests/second, but be polite
# enough to avoid WAF challenges. All tabs share one limiter.
try:
    S2_WEB_MIN_INTERVAL = max(0.0, float(os.environ.get("S2_WEB_MIN_INTERVAL", "3.2")))
except ValueError:
    S2_WEB_MIN_INTERVAL = 3.2
S2_RATE_LIMIT_COOLDOWN = 2.2  # shared pause after any 429 response
S2_WAF_COOLDOWN = 30.0  # longer pause after an AWS WAF challenge
S2_PAGE_SIZE = 1000
S2_BATCH_SIZE = 100
S2_MAX_OFFSET = 10_000
try:
    S2_TABS = max(1, int(os.environ.get("S2_TABS", "3")))
except ValueError:
    S2_TABS = 3
S2_HEADLESS = os.environ.get("S2_HEADLESS", "1").strip().lower() not in ("0", "false", "no")

# --- Semantic Scholar official Graph API -----------------------------------
S2_API_KEY = os.environ.get("S2_API_KEY", "").strip()
S2_API_BASE = "https://api.semanticscholar.org/graph/v1"
# The keyed allowance is 100 requests / 5 minutes. Stay at 1 req/s by default.
try:
    S2_API_MIN_INTERVAL = max(0.0, float(os.environ.get("S2_API_MIN_INTERVAL", "1.2")))
except ValueError:
    S2_API_MIN_INTERVAL = 1.2
S2_API_MAX_RETRIES = 5
S2_API_TIMEOUT = 30
try:
    S2_API_PAGE_SIZE = max(1, min(int(os.environ.get("S2_API_PAGE_SIZE", "500")), 1000))
except ValueError:
    S2_API_PAGE_SIZE = 500
S2_API_BATCH_SIZE = 100
S2_API_PAPER_FIELDS = "paperId,externalIds,title,authors,year,publicationDate"
S2_API_REF_FIELDS = os.environ.get(
    "S2_API_REF_FIELDS",
    ",".join(f"citedPaper.{f}" for f in S2_API_PAPER_FIELDS.split(",")),
)

MAX_RETRIES = 5
BACKOFF_BASE = 2.0  # seconds; wait = BACKOFF_BASE ** attempt
MAX_BACKOFF = 60.0
RETRY_JITTER = 0.5
JITTER = RETRY_JITTER
MAX_RETRY_AFTER = 120.0
REQUEST_TIMEOUT = 60  # seconds

# --- Crawl behaviour -------------------------------------------------------
MAX_PAPER_ATTEMPTS = 3
PROGRESS_INTERVAL_SECONDS = 30.0
STATS_FLUSH_EVERY = 50

# --- Identifiers -----------------------------------------------------------
# Characters replaced by ID_SEPARATOR in file names / fields (see clean_arxiv_id).
ID_SEPARATOR = "_"

def normalize_month(month: str) -> str:
    """Convert ``"2025-04"`` or ``"2504"`` to the arXiv ``"2504"`` form."""
    text = str(month).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        return match.group(1)[2:] + match.group(2)
    if re.fullmatch(r"\d{4}", text):
        return text
    raise ValueError(f"Unrecognised month {month!r}; expected '2025-04' or '2504'")


def _next_month(month: str) -> str:
    year, mon = int(month[:2]), int(month[2:])
    if mon == 12:
        year, mon = year + 1, 1
    else:
        mon += 1
    return f"{year:02d}{mon:02d}"


@dataclass(frozen=True)
class ArxivRange:
    """Inclusive range of new-style arXiv IDs ``YYMM.NNNNN``."""

    start_month: str
    start_id: int
    end_month: str
    end_id: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_month", normalize_month(self.start_month))
        object.__setattr__(self, "end_month", normalize_month(self.end_month))
        if (self.start_month, self.start_id) > (self.end_month, self.end_id):
            raise ValueError(f"Range starts after it ends: {self}")

    def iter_ids(self, month_sizes: Optional[Mapping[str, int]] = None) -> Iterator[str]:
        """Yield every arXiv ID of the range in order, e.g. ``"2504.12523"``.

        Args:
            month_sizes: Largest ID of each month, needed only for the months
                where the range does not end (i.e. when it crosses a month).
        """
        sizes = {normalize_month(k): v for k, v in (month_sizes or {}).items()}
        month = self.start_month
        while month <= self.end_month:
            first = self.start_id if month == self.start_month else 1
            if month == self.end_month:
                last = self.end_id
            elif month in sizes:
                last = sizes[month]
            else:
                raise ValueError(f"month_sizes is missing month {month}")
            for number in range(first, last + 1):
                yield f"{month}.{number:05d}"
            month = _next_month(month)

    def count(self, month_sizes: Optional[Mapping[str, int]] = None) -> int:
        """Number of IDs in the range."""
        return sum(1 for _ in self.iter_ids(month_sizes))


class _DecimalLoader(yaml.SafeLoader):
    """SafeLoader that keeps numbers as strings.

    YAML 1.1 treats unquoted IDs such as ``07642`` as *octal* (4002). arXiv IDs
    are decimal, so all scalars are loaded as strings and converted explicitly.
    """


_DecimalLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for _first_char, _resolvers in list(_DecimalLoader.yaml_implicit_resolvers.items()):
    _DecimalLoader.yaml_implicit_resolvers[_first_char] = [
        (tag, regexp) for tag, regexp in _resolvers if tag != "tag:yaml.org,2002:int"
    ]


def load_ranges_file(path: Path = RANGES_FILE) -> dict:
    """Load the raw content of ``ranges.yaml``."""
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_DecimalLoader) or {}


def load_member_range(member: Optional[str] = None, path: Path = RANGES_FILE) -> ArxivRange:
    """Return the ID range of one member.

    Args:
        member: Key under ``members`` in ``ranges.yaml``; defaults to the
            ``ARXIV_MEMBER`` environment variable.
    """
    member = member or os.environ.get("ARXIV_MEMBER")
    if not member:
        raise ValueError("Pass a member key or set the ARXIV_MEMBER environment variable")
    members = load_ranges_file(path).get("members", {})
    if member not in members:
        raise KeyError(f"{member!r} not found in {path.name}; available: {sorted(members)}")
    entry = members[member]
    fields = ("start_month", "start_id", "end_month", "end_id")
    missing = [name for name in fields if entry.get(name) is None]
    if missing:
        raise ValueError(f"{member}: fill in {missing} in {path.name}")
    return ArxivRange(
        start_month=entry["start_month"],
        start_id=int(entry["start_id"]),
        end_month=entry["end_month"],
        end_id=int(entry["end_id"]),
    )


def load_month_sizes(path: Path = RANGES_FILE) -> dict[str, int]:
    """Return the ``month_sizes`` table of ``ranges.yaml`` (may be empty)."""
    return {normalize_month(k): int(v) for k, v in (load_ranges_file(path).get("month_sizes") or {}).items()}
