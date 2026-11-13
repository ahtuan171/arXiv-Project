"""LaTeX cleanup and math normalization routines for Milestone 2."""
from __future__ import annotations

import re


def strip_latex_comments(text: str) -> str:
    r"""Remove unescaped LaTeX comments (% ... to end of line).
    
    Preserves escaped percent signs: \%.
    """
    lines = text.splitlines(keepends=True)
    cleaned_lines = []
    for line in lines:
        # Match % not preceded by an odd number of backslashes
        # A regex pattern using negative lookbehind: (?<!\\)(?:\\\\)*%
        # Or parse character by character for absolute safety:
        out = []
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "%":
                # count preceding backslashes
                bs_count = 0
                k = i - 1
                while k >= 0 and line[k] == "\\":
                    bs_count += 1
                    k -= 1
                if bs_count % 2 == 0:
                    # It's a real comment; skip the rest of the line (except newline)
                    if line.endswith("\n"):
                        out.append("\n")
                    break
            out.append(ch)
            i += 1
        cleaned_lines.append("".join(out))
    return "".join(cleaned_lines)


def normalize_math(text: str) -> str:
    r"""Normalize inline and block mathematics to standard forms.
    
    Inline math:
      - \( ... \) -> $ ... $
    
    Block math:
      - $$ ... $$ -> \begin{equation} ... \end{equation}
      - \[ ... \] -> \begin{equation} ... \end{equation}
      - \begin{equation*} ... \end{equation*} -> \begin{equation} ... \end{equation}
      - \begin{align|gather|multline}(*?) ... \end{...} -> \begin{equation} ... \end{equation}
    """
    # 1. Normalize block math environments
    # Replace $$ ... $$ with \begin{equation} ... \end{equation}
    text = re.sub(
        r"\$\$(.*?)\$\$",
        lambda m: f"\\begin{{equation}}\n{m.group(1).strip()}\n\\end{{equation}}",
        text,
        flags=re.DOTALL,
    )
    # Replace \[ ... \] with \begin{equation} ... \end{equation}
    text = re.sub(
        r"\\\[(.*?)\\\]",
        lambda m: f"\\begin{{equation}}\n{m.group(1).strip()}\n\\end{{equation}}",
        text,
        flags=re.DOTALL,
    )
    # Replace common AMS-LaTeX block math environments with standard \begin{equation}
    block_envs = ["equation\\*", "align", "align\\*", "gather", "gather\\*", "multline", "multline\\*", "flalign", "flalign\\*"]
    env_pattern = "|".join(block_envs)
    text = re.sub(
        rf"\\begin\{{({env_pattern})\}}(.*?)\\end\{{\1\}}",
        lambda m: f"\\begin{{equation}}\n{m.group(2).strip()}\n\\end{{equation}}",
        text,
        flags=re.DOTALL,
    )

    # 2. Normalize inline math \( ... \) -> $ ... $
    text = re.sub(r"\\\((.*?)\\\)", r"$\1$", text, flags=re.DOTALL)

    return text


def clean_latex_markup(text: str) -> str:
    r"""Remove non-semantic formatting commands, placement options, and spacing.
    
    - Strips placement options: \begin{figure}[htpb] -> \begin{figure}
    - Strips table lines: \midrule, \toprule, \bottomrule, \hline
    - Strips alignment commands: \centering, \raggedright, \raggedleft
    - Strips spacing commands: \vspace{...}, \hspace{...}, \bigskip, etc.
    - Strips layout commands: \noindent, \newpage, \clearpage
    - Strips \label{...} from body text
    """
    # Placement options in environments: e.g. \begin{figure}[htpb] or \begin{table}[!t]
    text = re.sub(r"(\\begin\{(?:figure|figure\*|table|table\*)\})\s*\[[^\]]*\]", r"\1", text)

    # Remove table rules
    text = re.sub(r"\\(?:midrule|toprule|bottomrule|hline)\b", "", text)

    # Remove alignment / positioning commands
    text = re.sub(r"\\(?:centering|raggedright|raggedleft|noindent|newpage|clearpage)\b", "", text)

    # Remove spacing commands
    text = re.sub(r"\\(?:vspace\*?|hspace\*?)\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:bigskip|medskip|smallskip|newline|linebreak)\b", "", text)

    # Remove \label{...} commands
    text = re.sub(r"\\label\{[^}]*\}", "", text)

    # Normalize multiple whitespace characters (excluding inside special blocks if needed)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def clean_text_content(text: str) -> str:
    """Convenience function: strip comments, normalize math, clean markup, and trim."""
    text = strip_latex_comments(text)
    text = normalize_math(text)
    text = clean_latex_markup(text)
    return text

