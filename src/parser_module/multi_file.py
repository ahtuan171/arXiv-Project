"""Multi-file gathering for LaTeX sources.

Identifies the root .tex file via \\documentclass and resolves \\input / \\include
transitively, ignoring commented-out commands and redundant auxiliary files.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from src.parser_module.cleaner import strip_latex_comments

log = logging.getLogger(__name__)

_INCLUDE_RE = re.compile(
    r"\\(?:input|include)\s*(?:\{([^}]+)\}|([^\s%{}]+))"
)


def find_root_tex_file(source_dir: Path) -> Path | None:
    """Find the root .tex file containing \\documentclass in the source directory.
    
    If multiple files match, prefers files named main.tex, ms.tex, paper.tex,
    or the file containing \\begin{document}.
    """
    if not source_dir.is_dir():
        return None

    tex_files = list(source_dir.glob("**/*.tex"))
    candidates: list[Path] = []

    for tex_file in tex_files:
        try:
            content = tex_file.read_text(encoding="utf-8", errors="replace")
            # Strip comments first so % \documentclass is ignored
            cleaned = strip_latex_comments(content)
            if re.search(r"\\(?:documentclass|documentstyle)\b", cleaned):
                candidates.append(tex_file)
        except Exception as exc:
            log.warning("Could not read %s: %s", tex_file, exc)

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Multiple candidates: prioritize by conventional names and \begin{document}
    priority_names = {"main.tex", "ms.tex", "paper.tex", "document.tex", f"{source_dir.name}.tex"}
    for c in candidates:
        if c.name.lower() in priority_names:
            return c

    # Next check which one has \begin{document}
    for c in candidates:
        try:
            content = strip_latex_comments(c.read_text(encoding="utf-8", errors="replace"))
            if r"\begin{document}" in content:
                return c
        except Exception:
            pass

    return candidates[0]


def resolve_include_path(base_file: Path, root_dir: Path, target_str: str) -> Path | None:
    """Resolve an \\input or \\include target to an existing file path."""
    target_str = target_str.strip().strip('"').strip("'")
    if not target_str:
        return None

    # Strip optional .tex extension to normalize
    clean_target = target_str[:-4] if target_str.endswith(".tex") else target_str

    candidates = [
        base_file.parent / f"{clean_target}.tex",
        base_file.parent / target_str,
        root_dir / f"{clean_target}.tex",
        root_dir / target_str,
    ]

    for p in candidates:
        if p.is_file():
            return p.resolve()

    return None


def gather_compilation_path(root_tex: Path) -> list[Path]:
    """Return the ordered list of .tex files included in the compilation path."""
    visited: set[Path] = set()
    order: list[Path] = []
    root_dir = root_tex.parent

    def _traverse(current_file: Path):
        canonical = current_file.resolve()
        if canonical in visited:
            return
        visited.add(canonical)
        order.append(canonical)

        try:
            content = current_file.read_text(encoding="utf-8", errors="replace")
            # Strip comments line by line so commented out \input is ignored
            uncommented = strip_latex_comments(content)
            for m in _INCLUDE_RE.finditer(uncommented):
                target_str = m.group(1) or m.group(2)
                resolved = resolve_include_path(current_file, root_dir, target_str)
                if resolved and resolved.suffix == ".tex":
                    _traverse(resolved)
        except Exception as exc:
            log.warning("Error traversing includes in %s: %s", current_file, exc)

    _traverse(root_tex)
    return order


def merge_tex_sources(root_tex: Path) -> str:
    """Merge all transitively included .tex files into a single consolidated source string.
    
    Ignores commented out \\input and \\include statements and redundant auxiliary files.
    """
    root_dir = root_tex.parent
    visited: set[Path] = set()

    def _inline_file(file_path: Path) -> str:
        canonical = file_path.resolve()
        if canonical in visited:
            return ""
        visited.add(canonical)

        try:
            raw_content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            log.warning("Could not read %s during inlining: %s", file_path, exc)
            return ""

        # Process line-by-line so we don't expand comments
        lines = raw_content.splitlines(keepends=True)
        expanded_lines = []
        for line in lines:
            # Check comment status of line
            uncommented = strip_latex_comments(line)
            m = _INCLUDE_RE.search(uncommented)
            if m:
                target_str = m.group(1) or m.group(2)
                resolved = resolve_include_path(file_path, root_dir, target_str)
                if resolved and resolved.suffix == ".tex":
                    # Replace include command with inlined content
                    inlined = _inline_file(resolved)
                    expanded_lines.append(f"\n% --- BEGIN INCLUDED: {resolved.name} ---\n")
                    expanded_lines.append(inlined)
                    expanded_lines.append(f"\n% --- END INCLUDED: {resolved.name} ---\n")
                    continue
            expanded_lines.append(line)

        return "".join(expanded_lines)

    return _inline_file(root_tex)

