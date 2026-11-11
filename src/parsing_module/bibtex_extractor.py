"""Milestone 2 Reference and BibTeX Extraction Module.

Extracts, standardizes, deduplicates, and structures bibliography entries
from LaTeX sources (.tex, .bbl, and .bib files) into the Milestone 2 `bibtex` table schema.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pandas as pd

# Schema column order specified in Milestone 2
BIBTEX_COLUMNS = [
    "arxiv_id",
    "citation_key",
    "entry_type",
    "title",
    "authors",
    "year",
    "doi",
    "journal",
    "url",
]


@dataclass
class BibtexEntry:
    """Represents a unified BibTeX reference entry according to Milestone 2 schema."""

    arxiv_id: str
    citation_key: str
    entry_type: str = "misc"
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    doi: str = ""
    journal: str = ""
    url: str = ""
    version: Optional[str] = None
    raw_text: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert entry to the 9-column dictionary conforming to Milestone 2 table."""
        return {
            "arxiv_id": str(self.arxiv_id).strip(),
            "citation_key": str(self.citation_key).strip(),
            "entry_type": str(self.entry_type or "misc").strip().lower(),
            "title": str(self.title or "").strip(),
            "authors": list(self.authors) if self.authors else [],
            "year": int(self.year) if self.year is not None else None,
            "doi": str(self.doi or "").strip(),
            "journal": str(self.journal or "").strip(),
            "url": str(self.url or "").strip(),
        }


# --- LaTeX Cleanup & Helpers ------------------------------------------------

_COMMENT_REGEX = re.compile(r"(?<!\\)%.*$")
_LATEX_COMMAND_CONTENT = re.compile(
    r"\\(?:textbf|textit|emph|underline|textsc|textsl|texttt|textmd|textnormal)\{([^}]*)\}"
)
_LATEX_FONT_SWITCH = re.compile(r"\{\\(?:em|it|bf|sl|sc|rm)\s+([^}]*)\}")
_URL_HREF_REGEX = re.compile(r"\\href\{([^}]+)\}(?:\{[^}]*\})?")
_URL_CMD_REGEX = re.compile(r"\\url\{([^}]+)\}")
_RAW_URL_REGEX = re.compile(r"https?://[^\s{}<>\"]+")
_DOI_REGEX = re.compile(
    r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*|doi\s+)?(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)",
    re.IGNORECASE,
)
_PAGE_RANGE_REGEX = re.compile(
    r"(?:pp\.?|pages?)\s*(?:\d+)?\s*(?:--|-|–)\s*(?:\d+)?", re.IGNORECASE
)
_YEAR_REGEX = re.compile(r"\b(19\d{2}|20\d{2})\b")

_LATEX_ACCENTS = [
    (re.compile(r"\\['`^\"~=.]\{?([A-Za-z])\}?"), r"\1"),
    (re.compile(r"\\c\{([A-Za-z])\}"), r"\1"),
    (re.compile(r"\\v\{([A-Za-z])\}"), r"\1"),
    (re.compile(r"\\u\{([A-Za-z])\}"), r"\1"),
    (re.compile(r"\\H\{([A-Za-z])\}"), r"\1"),
]

_LATEX_SPECIAL_CHARS = [
    (re.compile(r"\\&"), "&"),
    (re.compile(r"\\%"), "%"),
    (re.compile(r"\\_"), "_"),
    (re.compile(r"\\#"), "#"),
    (re.compile(r"\\\$"), "$"),
    (re.compile(r"\\\{"), "{"),
    (re.compile(r"\\\}"), "}"),
    (re.compile(r"---"), "—"),
    (re.compile(r"--"), "–"),
    (re.compile(r"~"), " "),
]


def clean_latex_text(text: str) -> str:
    """Clean LaTeX markup, formatting macros, accents, and comments from text."""
    if not text:
        return ""

    lines = []
    for line in text.splitlines():
        line = _COMMENT_REGEX.sub("", line)
        lines.append(line)
    cleaned = " ".join(lines)

    for _ in range(3):
        prev = cleaned
        cleaned = _LATEX_COMMAND_CONTENT.sub(r"\1", cleaned)
        cleaned = _LATEX_FONT_SWITCH.sub(r"\1", cleaned)
        if cleaned == prev:
            break

    for pattern, repl in _LATEX_ACCENTS:
        cleaned = pattern.sub(repl, cleaned)

    for pattern, repl in _LATEX_SPECIAL_CHARS:
        cleaned = pattern.sub(repl, cleaned)

    cleaned = re.sub(r"\\[a-zA-Z]+\b(?:\s*\{(?:\s*)\})?", " ", cleaned)
    cleaned = re.sub(r"[{}]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_author_list(raw_authors: str) -> list[str]:
    """Parse raw author string into a cleaned list of author names.

    Handles 'and' separated lists, 'Last, First' vs 'First Last',
    and removes 'others' / 'et al.'.
    """
    if not raw_authors:
        return []

    cleaned = clean_latex_text(raw_authors)
    cleaned = re.sub(r"\bet\s+al\.?\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bothers\b", "", cleaned, flags=re.IGNORECASE)

    raw_list = re.split(r"\s+and\s+|\s*;\s*", cleaned, flags=re.IGNORECASE)
    if len(raw_list) == 1 and "," in raw_list[0] and raw_list[0].count(",") > 1:
        parts = [p.strip() for p in raw_list[0].split(",") if p.strip()]
        if all(len(p.split()) <= 3 for p in parts):
            raw_list = parts

    authors: list[str] = []
    for item in raw_list:
        name = item.strip().strip(",")
        if not name:
            continue
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            if len(parts) == 2 and parts[1]:
                name = f"{parts[1]} {parts[0]}"
            else:
                name = parts[0]
        name = re.sub(r"\s+", " ", name).strip()
        if name and name.lower() not in ("and", "&", "others", "et al"):
            authors.append(name)
    return authors


def extract_doi(text: str) -> str:
    """Extract standard DOI string (e.g. 10.xxxx/yyyy) from text."""
    if not text:
        return ""
    m = _DOI_REGEX.search(text)
    if m:
        doi = m.group(1).strip()
        return doi.rstrip(".,;)}]")
    return ""


def extract_url(text: str) -> str:
    """Extract URL from \\url{}, \\href{}, or raw http(s) link."""
    if not text:
        return ""
    m = _URL_CMD_REGEX.search(text)
    if m:
        return m.group(1).strip()
    m = _URL_HREF_REGEX.search(text)
    if m:
        return m.group(1).strip()
    m = _RAW_URL_REGEX.search(text)
    if m:
        return m.group(0).rstrip(".,;)}]")
    return ""


def extract_year(text: str) -> Optional[int]:
    """Extract publication year (1900-2099), excluding page ranges."""
    if not text:
        return None

    masked = _PAGE_RANGE_REGEX.sub(" ", text)
    bracket_match = re.search(r"\((\b(?:19\d{2}|20\d{2})\b)\)", masked)
    if bracket_match:
        return int(bracket_match.group(1))

    candidates = _YEAR_REGEX.findall(masked)
    if candidates:
        return int(candidates[-1])
    return None


# --- \\bibitem Parsing -------------------------------------------------------

_BIBITEM_REGEX = re.compile(
    r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}",
    re.MULTILINE,
)
_QUOTE_TITLE_REGEX = re.compile(r"(?:``|\"|“)(.*?)(?:’’|''|\"|”)", re.DOTALL)
_EMPH_REGEX = re.compile(r"\\(?:emph|textit)\{([^}]*)\}|\{\\em\s+([^}]*)\}")


def _infer_entry_type(journal: str, text: str) -> str:
    """Infer BibTeX entry type from venue name and raw content."""
    combined = (journal + " " + text).lower()
    if any(
        k in combined
        for k in (
            "proceedings",
            "conference",
            "in ",
            "cvpr",
            "icml",
            "neurips",
            "iclr",
            "aaai",
            "ijcai",
            "acl",
            "emnlp",
            "symposium",
            "workshop",
        )
    ):
        return "inproceedings"
    if any(k in combined for k in ("thesis", "dissertation")):
        return "phdthesis"
    if any(k in combined for k in ("book", "press", "springer", "wiley", "cambridge", "routledge")):
        return "book"
    if any(k in combined for k in ("arxiv", "preprint", "techreport")):
        return "misc"
    if journal.strip():
        return "article"
    return "misc"


def extract_bibitem_entries(
    tex_content: str,
    arxiv_id: str = "",
    version: Optional[str] = None,
) -> list[BibtexEntry]:
    """Extract reference entries from LaTeX \\bibitem commands."""
    if not tex_content:
        return []

    # Limit search to thebibliography if present
    env_match = re.search(
        r"\\begin\{thebibliography\}(?:\{[^}]*\})?(.*?)\\end\{thebibliography\}",
        tex_content,
        re.DOTALL,
    )
    search_text = env_match.group(1) if env_match else tex_content

    matches = list(_BIBITEM_REGEX.finditer(search_text))
    if not matches:
        return []

    entries: list[BibtexEntry] = []
    for idx, match in enumerate(matches):
        key = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(search_text)
        item_raw = search_text[start:end].strip()

        doi = extract_doi(item_raw)
        url = extract_url(item_raw)
        year = extract_year(item_raw)

        # Check for \newblock delimiters (standard BibTeX output)
        title = ""
        authors: list[str] = []
        journal = ""

        if r"\newblock" in item_raw:
            blocks = [b.strip() for b in re.split(r"\\newblock", item_raw) if b.strip()]
            if len(blocks) >= 2:
                authors = parse_author_list(blocks[0])
                title = clean_latex_text(blocks[1])
                if len(blocks) >= 3:
                    journal = clean_latex_text(blocks[2])
                    # Clean out leading "In " or "in " from venue
                    journal = re.sub(r"^(?:in\s+)+", "", journal, flags=re.IGNORECASE).strip()
            elif len(blocks) == 1:
                title = clean_latex_text(blocks[0])

        # If title not found via \newblock, check for quotes
        if not title:
            q_match = _QUOTE_TITLE_REGEX.search(item_raw)
            if q_match:
                title = clean_latex_text(q_match.group(1))
                prefix = item_raw[: q_match.start()]
                authors = parse_author_list(prefix)
                suffix = item_raw[q_match.end() :]
                emph_m = _EMPH_REGEX.search(suffix)
                if emph_m:
                    journal = clean_latex_text(emph_m.group(1) or emph_m.group(2) or "")
                else:
                    journal = clean_latex_text(suffix)

        # Fallback: check for \emph (journal) and split around it
        if not title:
            emph_m = _EMPH_REGEX.search(item_raw)
            if emph_m:
                journal = clean_latex_text(emph_m.group(1) or emph_m.group(2) or "")
                prefix = item_raw[: emph_m.start()].strip()
                # Split prefix by sentence/dot or year
                sentences = [s.strip() for s in re.split(r"\.\s+", prefix) if s.strip()]
                if len(sentences) >= 2:
                    authors = parse_author_list(sentences[0])
                    title = clean_latex_text(". ".join(sentences[1:]))
                elif len(sentences) == 1:
                    title = clean_latex_text(sentences[0])
            else:
                # Fallback to sentence split
                sentences = [
                    s.strip()
                    for s in re.split(r"\.\s+", clean_latex_text(item_raw))
                    if s.strip()
                ]
                if len(sentences) >= 2:
                    authors = parse_author_list(sentences[0])
                    title = sentences[1]
                    if len(sentences) >= 3:
                        journal = ". ".join(sentences[2:])
                elif len(sentences) == 1:
                    title = sentences[0]

        # Clean trailing punctuation from title and journal
        title = title.rstrip(".,; ")
        journal = re.sub(r"^(?:in\s+)+", "", journal, flags=re.IGNORECASE).rstrip(".,; ")
        entry_type = _infer_entry_type(journal, item_raw)

        entries.append(
            BibtexEntry(
                arxiv_id=arxiv_id,
                citation_key=key,
                entry_type=entry_type,
                title=title,
                authors=authors,
                year=year,
                doi=doi,
                journal=journal,
                url=url,
                version=version,
                raw_text=item_raw,
            )
        )

    return entries


# --- .bib File Parsing -------------------------------------------------------

_BIB_ENTRY_START = re.compile(r"@([a-zA-Z]+)\s*\{", re.MULTILINE)


def _parse_bib_fields(body: str) -> dict[str, str]:
    """Parse field = value assignments inside a BibTeX entry body."""
    fields: dict[str, str] = {}
    i = 0
    n = len(body)

    while i < n:
        # Skip whitespace and commas
        while i < n and body[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break

        # Read field name
        name_match = re.match(r"([a-zA-Z0-9_\-]+)\s*=", body[i:])
        if not name_match:
            # Skip until next comma or line
            while i < n and body[i] not in ",\n":
                i += 1
            if i < n and body[i] == ",":
                i += 1
            continue

        field_name = name_match.group(1).lower().strip()
        i += name_match.end()

        # Skip whitespace before value
        while i < n and body[i] in " \t\r\n":
            i += 1
        if i >= n:
            break

        val = ""
        if body[i] == "{":
            # Brace delimited: count braces
            brace_depth = 1
            i += 1
            start_v = i
            while i < n and brace_depth > 0:
                if body[i] == "{" and (i == 0 or body[i - 1] != "\\"):
                    brace_depth += 1
                elif body[i] == "}" and (i == 0 or body[i - 1] != "\\"):
                    brace_depth -= 1
                i += 1
            val = body[start_v : i - 1 if brace_depth == 0 else i]
        elif body[i] == '"':
            # Quote delimited: read until next unescaped quote
            i += 1
            start_v = i
            while i < n:
                if body[i] == '"' and (i == 0 or body[i - 1] != "\\"):
                    break
                i += 1
            val = body[start_v:i]
            if i < n and body[i] == '"':
                i += 1
        else:
            # Number or bare word: read until comma or newline or closing brace
            start_v = i
            while i < n and body[i] not in ",\r\n}":
                i += 1
            val = body[start_v:i].strip()

        fields[field_name] = val.strip()

        # Advance to comma
        while i < n and body[i] in " \t\r\n":
            i += 1
        if i < n and body[i] == ",":
            i += 1

    return fields


def extract_bib_file_entries(
    bib_content: str,
    arxiv_id: str = "",
    version: Optional[str] = None,
) -> list[BibtexEntry]:
    """Parse standard BibTeX entries from .bib file content."""
    if not bib_content:
        return []

    entries: list[BibtexEntry] = []
    pos = 0
    n = len(bib_content)

    while pos < n:
        match = _BIB_ENTRY_START.search(bib_content, pos)
        if not match:
            break

        entry_type = match.group(1).strip().lower()
        start_brace = match.end() - 1  # index of '{'

        # Balance braces to find entire entry
        brace_depth = 1
        i = start_brace + 1
        while i < n and brace_depth > 0:
            if bib_content[i] == "{" and (i == 0 or bib_content[i - 1] != "\\"):
                brace_depth += 1
            elif bib_content[i] == "}" and (i == 0 or bib_content[i - 1] != "\\"):
                brace_depth -= 1
            i += 1

        entry_raw = bib_content[start_brace + 1 : i - 1 if brace_depth == 0 else i]
        pos = i

        # Skip non-publication types
        if entry_type in ("comment", "string", "preamble"):
            continue

        # Split citation_key from fields
        if "," not in entry_raw:
            continue
        key, body = entry_raw.split(",", 1)
        citation_key = key.strip()
        if not citation_key:
            continue

        fields = _parse_bib_fields(body)

        title = clean_latex_text(fields.get("title", ""))
        raw_authors = fields.get("author") or fields.get("authors") or ""
        authors = parse_author_list(raw_authors)

        year = extract_year(fields.get("year", ""))
        doi = extract_doi(fields.get("doi", ""))
        journal = clean_latex_text(
            fields.get("journal")
            or fields.get("booktitle")
            or fields.get("publisher")
            or ""
        )
        url = extract_url(fields.get("url", ""))
        if not url:
            eprint = fields.get("eprint", "").strip()
            archive_prefix = fields.get("archiveprefix", "").lower().strip()
            if eprint and (archive_prefix == "arxiv" or re.match(r"^\d{4}\.\d{4,5}$", eprint)):
                url = f"https://arxiv.org/abs/{eprint}"

        entries.append(
            BibtexEntry(
                arxiv_id=arxiv_id,
                citation_key=citation_key,
                entry_type=entry_type,
                title=title,
                authors=authors,
                year=year,
                doi=doi,
                journal=journal,
                url=url,
                version=version,
                raw_text=bib_content[match.start() : i],
            )
        )

    return entries


# --- Compilation Path & Multi-file Gathering ---------------------------------

_INPUT_INCLUDE_REGEX = re.compile(r"\\(?:input|include)\{([^}]+)\}")
_BIBLIOGRAPHY_REGEX = re.compile(r"\\bibliography\{([^}]+)\}")
_DOCUMENTCLASS_REGEX = re.compile(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{[^}]+\}")


def _strip_comments_from_tex(content: str) -> str:
    """Remove LaTeX comments line by line while preserving newlines."""
    lines = []
    for line in content.splitlines():
        line = _COMMENT_REGEX.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def _find_root_tex_file(version_dir: Path) -> Optional[Path]:
    """Find the root .tex file containing \\documentclass."""
    candidates: list[tuple[int, Path]] = []
    all_tex = list(version_dir.glob("*.tex")) + list(version_dir.glob("*/*.tex"))
    # Remove duplicates preserving order
    unique_tex: list[Path] = []
    seen = set()
    for p in all_tex:
        res = p.resolve()
        if res not in seen and p.is_file():
            seen.add(res)
            unique_tex.append(p)

    for p in unique_tex:
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
            uncommented = _strip_comments_from_tex(content)
            if _DOCUMENTCLASS_REGEX.search(uncommented):
                score = 0
                if p.name.lower() in ("main.tex", "paper.tex", "ms.tex", "article.tex"):
                    score += 10
                if p.parent == version_dir:
                    score += 5
                score += min(len(content) // 1000, 10)
                candidates.append((score, p))
        except Exception:
            continue

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # Fallback to main.tex or first available .tex
    for p in unique_tex:
        if p.name.lower() == "main.tex":
            return p
    return unique_tex[0] if unique_tex else None


def find_compilation_files(version_dir: Path) -> tuple[list[Path], list[Path]]:
    """Identify the main compilation path and associated .bib files.

    Returns:
        (compilation_tex_files, bib_files)
    """
    version_dir = Path(version_dir)
    if not version_dir.exists():
        return [], []

    root_tex = _find_root_tex_file(version_dir)
    if not root_tex:
        # No tex files found
        bib_files = list(version_dir.glob("*.bib")) + list(version_dir.glob("*/*.bib"))
        return [], bib_files

    reachable_tex: list[Path] = []
    visited: set[Path] = set()
    referenced_bib_names: set[str] = set()

    def _traverse(tex_path: Path):
        canonical = tex_path.resolve()
        if canonical in visited or not tex_path.is_file():
            return
        visited.add(canonical)
        reachable_tex.append(tex_path)

        try:
            raw_text = tex_path.read_text(encoding="utf-8", errors="ignore")
            content = _strip_comments_from_tex(raw_text)
        except Exception:
            return

        # Find \bibliography{...}
        for m in _BIBLIOGRAPHY_REGEX.finditer(content):
            for name in m.group(1).split(","):
                name = name.strip()
                if name:
                    if name.endswith(".bib"):
                        name = name[:-4]
                    referenced_bib_names.add(name.lower())

        # Find \input{...} and \include{...}
        for m in _INPUT_INCLUDE_REGEX.finditer(content):
            target = m.group(1).strip()
            if not target:
                continue
            if not target.endswith(".tex"):
                target_name = f"{target}.tex"
            else:
                target_name = target

            # Try relative to current tex file directory
            candidate = tex_path.parent / target_name
            if not candidate.is_file():
                # Try relative to version_dir root
                candidate = version_dir / target_name
            if candidate.is_file():
                _traverse(candidate)

    _traverse(root_tex)

    # Resolve .bib files
    bib_files: list[Path] = []
    all_bib = list(version_dir.glob("*.bib")) + list(version_dir.glob("*/*.bib"))
    bib_map: dict[str, Path] = {}
    for b in all_bib:
        stem = b.stem.lower()
        bib_map[stem] = b

    if referenced_bib_names:
        for ref in referenced_bib_names:
            if ref in bib_map and bib_map[ref] not in bib_files:
                bib_files.append(bib_map[ref])

    # If no referenced bib files matched or none referenced, include any found in dir
    if not bib_files and all_bib:
        bib_files.extend(all_bib)

    return reachable_tex, bib_files


def extract_version_references(
    version_dir: Path,
    arxiv_id: str = "",
    version: Optional[str] = None,
) -> list[BibtexEntry]:
    """Extract all reference entries from the compilation path of a single paper version."""
    version_dir = Path(version_dir)
    tex_files, bib_files = find_compilation_files(version_dir)

    entries_map: dict[str, BibtexEntry] = {}

    # 1. Parse .bib files (typically the most structured data)
    for bib in bib_files:
        try:
            content = bib.read_text(encoding="utf-8", errors="ignore")
            for entry in extract_bib_file_entries(content, arxiv_id=arxiv_id, version=version):
                key = entry.citation_key.strip()
                if key:
                    entries_map[key] = entry
        except Exception:
            continue

    # 2. Parse reachable .tex files
    for tex in tex_files:
        try:
            content = tex.read_text(encoding="utf-8", errors="ignore")
            for entry in extract_bibitem_entries(content, arxiv_id=arxiv_id, version=version):
                key = entry.citation_key.strip()
                if not key:
                    continue
                if key not in entries_map:
                    entries_map[key] = entry
                else:
                    # Unionize fields if bibitem has info that bib file lacked
                    existing = entries_map[key]
                    if not existing.title and entry.title:
                        existing.title = entry.title
                    if not existing.authors and entry.authors:
                        existing.authors = entry.authors
                    if existing.year is None and entry.year is not None:
                        existing.year = entry.year
                    if not existing.doi and entry.doi:
                        existing.doi = entry.doi
                    if not existing.journal and entry.journal:
                        existing.journal = entry.journal
                    if not existing.url and entry.url:
                        existing.url = entry.url
        except Exception:
            continue

    # 3. Check for .bbl files
    for bbl in list(version_dir.glob("*.bbl")) + list(version_dir.glob("*/*.bbl")):
        try:
            content = bbl.read_text(encoding="utf-8", errors="ignore")
            for entry in extract_bibitem_entries(content, arxiv_id=arxiv_id, version=version):
                key = entry.citation_key.strip()
                if not key:
                    continue
                if key not in entries_map:
                    entries_map[key] = entry
                else:
                    existing = entries_map[key]
                    if not existing.doi and entry.doi:
                        existing.doi = entry.doi
                    if not existing.url and entry.url:
                        existing.url = entry.url
                    if existing.year is None and entry.year is not None:
                        existing.year = entry.year
        except Exception:
            continue

    return list(entries_map.values())


# --- Cross-Version Deduplication & Merging -----------------------------------

def _normalize_title_for_match(title: str) -> str:
    """Normalize title for fuzzy/exact cross-version comparison."""
    if not title:
        return ""
    t = re.sub(r"\$[^$]*\$", "", title)
    t = re.sub(r"[^a-zA-Z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _normalize_doi(doi: str) -> str:
    """Normalize DOI string."""
    return doi.strip().lower().rstrip(".,;)}]")


def _extract_arxiv_id_from_url(url: str) -> str:
    """Extract arXiv ID from URL."""
    if not url:
        return ""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", url, re.IGNORECASE)
    if m:
        return re.sub(r"v\d+$", "", m.group(1)).lower()
    return ""


def _title_jaccard_similarity(t1: str, t2: str) -> float:
    """Calculate token Jaccard similarity between two normalized titles."""
    words1 = set(t1.split())
    words2 = set(t2.split())
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)


@dataclass
class _BibtexCluster:
    canonical_key: str
    keys_seen: set[str] = field(default_factory=set)
    entry: BibtexEntry = field(default_factory=lambda: BibtexEntry("", ""))


def deduplicate_bibtex_entries(
    entries_by_version: Mapping[str, list[BibtexEntry]] | Sequence[BibtexEntry],
    arxiv_id: str = "",
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Deduplicate references across versions and unionize their fields.

    Matches entries by:
      1. Matching valid DOIs.
      2. Matching arXiv IDs in URL.
      3. Matching normalized titles (with high confidence or matching author/year).
      4. Matching citation keys with compatible titles.

    Returns:
        (df, rename_map):
            df: pandas DataFrame conforming to Milestone 2 `bibtex` schema.
            rename_map: dict mapping dropped citation keys to their canonical key.
    """
    ordered_entries: list[BibtexEntry] = []

    if isinstance(entries_by_version, Mapping):
        # Sort versions chronologically (v1, v2, v3...)
        def _version_sort_key(ver_name: str) -> int:
            m = re.search(r"\d+", str(ver_name))
            return int(m.group(0)) if m else 999999

        sorted_ver_keys = sorted(entries_by_version.keys(), key=_version_sort_key)
        for ver in sorted_ver_keys:
            ordered_entries.extend(entries_by_version[ver])
    else:
        ordered_entries = list(entries_by_version)

    clusters: list[_BibtexCluster] = []
    rename_map: dict[str, str] = {}

    for entry in ordered_entries:
        curr_doi = _normalize_doi(entry.doi)
        curr_arxiv_url = _extract_arxiv_id_from_url(entry.url)
        curr_title_norm = _normalize_title_for_match(entry.title)
        curr_key = entry.citation_key.strip()

        matched_cluster: Optional[_BibtexCluster] = None

        for cluster in clusters:
            c_entry = cluster.entry
            c_doi = _normalize_doi(c_entry.doi)
            c_arxiv_url = _extract_arxiv_id_from_url(c_entry.url)
            c_title_norm = _normalize_title_for_match(c_entry.title)

            # Match 1: Same DOI
            if curr_doi and c_doi and curr_doi == c_doi:
                matched_cluster = cluster
                break

            # Match 2: Same arXiv ID in URL
            if curr_arxiv_url and c_arxiv_url and curr_arxiv_url == c_arxiv_url:
                matched_cluster = cluster
                break

            # Match 3: Normalized title exact match (if reasonably long)
            if (
                curr_title_norm
                and c_title_norm
                and curr_title_norm == c_title_norm
                and len(curr_title_norm) >= 8
            ):
                matched_cluster = cluster
                break

            # Match 4: Same citation key and compatible titles
            if curr_key and (curr_key == cluster.canonical_key or curr_key in cluster.keys_seen):
                if not curr_title_norm or not c_title_norm:
                    matched_cluster = cluster
                    break
                sim = _title_jaccard_similarity(curr_title_norm, c_title_norm)
                if sim >= 0.5:
                    matched_cluster = cluster
                    break

            # Match 5: High title similarity with same year or matching first author
            if curr_title_norm and c_title_norm:
                sim = _title_jaccard_similarity(curr_title_norm, c_title_norm)
                if sim >= 0.8:
                    if entry.year is not None and c_entry.year is not None and entry.year == c_entry.year:
                        matched_cluster = cluster
                        break
                    if entry.authors and c_entry.authors:
                        a1 = entry.authors[0].lower().split()[-1]
                        a2 = c_entry.authors[0].lower().split()[-1]
                        if a1 == a2:
                            matched_cluster = cluster
                            break

        if matched_cluster is not None:
            # Unionize fields
            target = matched_cluster.entry

            # Title: keep the longer/more informative
            if len(entry.title) > len(target.title):
                target.title = entry.title

            # Authors: unionize or keep more complete list
            if not target.authors and entry.authors:
                target.authors = list(entry.authors)
            elif entry.authors and target.authors:
                if len(entry.authors) > len(target.authors):
                    target.authors = list(entry.authors)
                elif len(entry.authors) == len(target.authors):
                    # Prefer longer representation (e.g. full names vs initials)
                    if sum(len(a) for a in entry.authors) > sum(len(a) for a in target.authors):
                        target.authors = list(entry.authors)

            # Year
            if target.year is None and entry.year is not None:
                target.year = entry.year

            # DOI
            if not target.doi and entry.doi:
                target.doi = entry.doi

            # Journal
            if not target.journal and entry.journal:
                target.journal = entry.journal
            elif len(entry.journal) > len(target.journal):
                target.journal = entry.journal

            # URL
            if not target.url and entry.url:
                target.url = entry.url

            # Entry type: prefer non-misc
            if target.entry_type == "misc" and entry.entry_type != "misc":
                target.entry_type = entry.entry_type

            # Record citation key mapping
            if curr_key != matched_cluster.canonical_key:
                rename_map[curr_key] = matched_cluster.canonical_key
            matched_cluster.keys_seen.add(curr_key)

        else:
            # Create a new cluster
            new_cluster = _BibtexCluster(
                canonical_key=curr_key,
                keys_seen={curr_key},
                entry=BibtexEntry(
                    arxiv_id=entry.arxiv_id or arxiv_id,
                    citation_key=curr_key,
                    entry_type=entry.entry_type,
                    title=entry.title,
                    authors=list(entry.authors),
                    year=entry.year,
                    doi=entry.doi,
                    journal=entry.journal,
                    url=entry.url,
                    version=entry.version,
                    raw_text=entry.raw_text,
                ),
            )
            clusters.append(new_cluster)

    # Build final DataFrame
    records = []
    for cluster in clusters:
        e = cluster.entry
        if arxiv_id and not e.arxiv_id:
            e.arxiv_id = arxiv_id
        records.append(e.to_dict())

    if not records:
        df = pd.DataFrame(columns=BIBTEX_COLUMNS)
    else:
        df = pd.DataFrame(records)
        df = df[BIBTEX_COLUMNS]

    # Convert year to nullable integer (pd.Int64)
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")

    # Ensure other string columns are clean strings
    str_cols = ["arxiv_id", "citation_key", "entry_type", "title", "doi", "journal", "url"]
    for col in str_cols:
        df[col] = df[col].fillna("").astype(str)

    return df, rename_map


# --- Publication-Level Pipeline & Export -------------------------------------

def process_publication_references(
    pub_dir: Path | str,
    arxiv_id: str = "",
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Process all versions of a publication directory, extracting and deduplicating references.

    Args:
        pub_dir: Path to publication directory containing version subdirectories (v1, v2...)
                 or directly containing source files.
        arxiv_id: Publication arXiv ID. If empty, tries to infer from directory name.

    Returns:
        (bibtex_df, rename_map)
    """
    pub_path = Path(pub_dir)
    if not arxiv_id:
        arxiv_id = pub_path.name

    version_dirs = [
        d
        for d in pub_path.iterdir()
        if d.is_dir() and re.match(r"^v\d+$", d.name, re.IGNORECASE)
    ]

    entries_by_ver: dict[str, list[BibtexEntry]] = {}
    if version_dirs:
        def _v_num(p: Path) -> int:
            m = re.search(r"\d+", p.name)
            return int(m.group(0)) if m else 0

        for v_dir in sorted(version_dirs, key=_v_num):
            entries = extract_version_references(v_dir, arxiv_id=arxiv_id, version=v_dir.name)
            entries_by_ver[v_dir.name] = entries
    else:
        entries = extract_version_references(pub_path, arxiv_id=arxiv_id, version="v1")
        entries_by_ver["v1"] = entries

    return deduplicate_bibtex_entries(entries_by_ver, arxiv_id=arxiv_id)


def export_bibtex_table(
    df: pd.DataFrame,
    out_path: Path | str,
    format: str = "parquet",
) -> None:
    """Export bibtex DataFrame to parquet, csv, or jsonl."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fmt = format.lower()
    if fmt == "parquet" or out.suffix.lower() == ".parquet":
        df.to_parquet(out, index=False)
    elif fmt == "csv" or out.suffix.lower() == ".csv":
        csv_df = df.copy()
        csv_df["authors"] = csv_df["authors"].apply(
            lambda a: json.dumps(a, ensure_ascii=False) if isinstance(a, list) else str(a)
        )
        csv_df.to_csv(out, index=False, encoding="utf-8")
    elif fmt == "jsonl" or out.suffix.lower() == ".jsonl":
        df.to_json(out, orient="records", lines=True, force_ascii=False)
    else:
        raise ValueError(f"Unsupported format: {format}")





