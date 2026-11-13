"""Sentence tokenization for LaTeX prose avoiding splitting on math, commands, abbreviations, and decimals."""
from __future__ import annotations

import re

# Protected abbreviation pattern
ABBREVIATIONS = (
    r"i\.e\.|e\.g\.|et al\.|etc\.|cf\.|vs\.|viz\.|dr\.|prof\.|mr\.|mrs\.|ms\.|"
    r"fig\.|figs\.|tab\.|tabs\.|sec\.|secs\.|eq\.|eqs\.|ref\.|refs\.|app\.|no\.|vol\."
)
_ABBR_RE = re.compile(rf"\b({ABBREVIATIONS})", flags=re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", flags=re.IGNORECASE)
_DECIMAL_RE = re.compile(r"(?<=\d)\.(?=\d)")
_ELLIPSIS_RE = re.compile(r"\.{2,}")
_INLINE_MATH_RE = re.compile(r"\$[^\$]+\$")
_CMD_BRACES_RE = re.compile(r"\\[a-zA-Z]+(?:\*|\b)(?:\[[^\]]*\])?(?:\{[^{}]*\})*")

_PLACEHOLDER_DOT = "\uE000"
_PLACEHOLDER_ELLIPSIS = "\uE001"


def split_sentences(text: str) -> list[str]:
    r"""Split prose text into sentences according to Milestone 2 requirements.
    
    Leaves are sentences separated by periods/dots.
    Does NOT split inside:
      - mathematics ($...$)
      - LaTeX commands and arguments (\cmd{...}, \cite{...})
      - abbreviations (i.e., e.g., et al., Fig. 1, etc.)
      - URLs
      - decimal numbers (e.g. 3.14)
      - ellipses (...)
    """
    if not text or not text.strip():
        return []

    # Working copy
    s = text

    # Step 1: Protect URLs
    def _mask_urls(m):
        return m.group(0).replace(".", _PLACEHOLDER_DOT)
    s = _URL_RE.sub(_mask_urls, s)

    # Step 2: Protect inline math
    def _mask_math(m):
        return m.group(0).replace(".", _PLACEHOLDER_DOT)
    s = _INLINE_MATH_RE.sub(_mask_math, s)

    # Step 3: Protect LaTeX commands and brace arguments
    def _mask_cmds(m):
        return m.group(0).replace(".", _PLACEHOLDER_DOT)
    s = _CMD_BRACES_RE.sub(_mask_cmds, s)

    # Step 4: Protect ellipses
    s = _ELLIPSIS_RE.sub(_PLACEHOLDER_ELLIPSIS, s)

    # Step 5: Protect abbreviations
    def _mask_abbr(m):
        return m.group(0).replace(".", _PLACEHOLDER_DOT)
    s = _ABBR_RE.sub(_mask_abbr, s)

    # Step 6: Protect decimals
    s = _DECIMAL_RE.sub(_PLACEHOLDER_DOT, s)

    # Now split on sentence terminators: dot, question mark, exclamation mark,
    # followed by whitespace or at end of string.
    raw_sentences = re.split(r"(?<=[.!?])(?:\s+|\n+)", s)

    sentences = []
    for sent in raw_sentences:
        # Restore placeholders
        sent = sent.replace(_PLACEHOLDER_DOT, ".")
        sent = sent.replace(_PLACEHOLDER_ELLIPSIS, "...")
        sent = sent.strip()
        if sent:
            sentences.append(sent)

    return sentences

