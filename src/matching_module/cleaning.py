"""Text cleaning and normalization utilities for Milestone 3 Reference Matching."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "via", "with",
})

_ARXIV_NEW_RE = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
_ARXIV_OLD_RE = re.compile(r"\b([a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")


def fold_accents(text: str) -> str:
    """Fold Unicode accents (e.g. Ruciński -> Rucinski) to ASCII."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_text(text: Any) -> str:
    """Lowercase, fold accents, strip LaTeX markup/punctuation, and collapse spaces."""
    if text is None:
        return ""
    s = str(text)
    if not s or s.lower() == "nan":
        return ""
    s = fold_accents(s).lower()
    s = _LATEX_CMD_RE.sub(" ", s)
    s = s.replace("{", " ").replace("}", " ").replace("$", " ")
    s = _NON_ALNUM_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_title(title: Any) -> str:
    """Normalize a paper title for string comparison."""
    return normalize_text(title)


def title_tokens(title: Any, *, remove_stopwords: bool = True) -> list[str]:
    """Tokenize a cleaned title, optionally removing English stopwords."""
    cleaned = clean_title(title)
    if not cleaned:
        return []
    toks = cleaned.split()
    if remove_stopwords:
        filtered = [t for t in toks if t not in STOPWORDS]
        return filtered if filtered else toks
    return toks


def extract_surnames(authors: Any) -> list[str]:
    """Extract normalized lowercase surnames from an author list or string."""
    if authors is None:
        return []
    if isinstance(authors, str):
        raw_list = [a.strip() for a in re.split(r"\band\b|;|,", authors) if a.strip()]
    elif hasattr(authors, "__iter__"):
        raw_list = [str(a).strip() for a in authors if a is not None and str(a).strip()]
    else:
        return []

    surnames: list[str] = []
    for name in raw_list:
        folded = fold_accents(name).lower().strip()
        if "," in folded:
            # Format: "Surname, Firstname"
            part = folded.split(",", 1)[0].strip()
        else:
            # Format: "Firstname [Middle] Surname"
            cleaned = _NON_ALNUM_RE.sub(" ", folded).strip()
            tokens = cleaned.split()
            part = tokens[-1] if tokens else ""
        part = _NON_ALNUM_RE.sub("", part)
        if part and len(part) >= 2:
            surnames.append(part)
    return surnames


def extract_year_int(val: Any) -> int | None:
    """Extract a 4-digit publication/submission year as int, or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            if val != val:  # NaN
                return None
            y = int(val)
            return y if 1900 <= y <= 2100 else None
        except (ValueError, TypeError):
            return None
    m = _YEAR_RE.search(str(val))
    return int(m.group(1)) if m else None


def extract_arxiv_id_from_text(*values: Any) -> str | None:
    """Extract a normalized arXiv ID (e.g. '2501.07642') from URL, DOI, or journal strings."""
    for val in values:
        if val is None:
            continue
        s = str(val).strip()
        if not s:
            continue
        m_new = _ARXIV_NEW_RE.search(s)
        if m_new:
            return m_new.group(1)
        m_old = _ARXIV_OLD_RE.search(s)
        if m_old:
            return m_old.group(1).lower()
    return None
