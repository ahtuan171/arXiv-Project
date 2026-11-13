"""LaTeX to Hierarchical Tree Parser Module (Milestone 2).

Converts LaTeX source files into structured hierarchical trees:
  - Multi-file compilation gathering (resolves \\documentclass, \\input, \\include)
  - Math normalization & LaTeX cleanup
  - Tree construction: Document -> Headings -> Lists -> Items -> Leaf Elements (Sentences, Formulas, Figures)
"""

from src.parser_module.cleaner import (
    clean_latex_markup,
    clean_text_content,
    normalize_math,
    strip_latex_comments,
)
from src.parser_module.hierarchy_builder import (
    HierarchyBuilder,
    HierarchyNode,
    LaTeXHierarchyTree,
    parse_latex_to_hierarchy,
)
from src.parser_module.multi_file import (
    find_root_tex_file,
    gather_compilation_path,
    merge_tex_sources,
)
from src.parser_module.parser import (
    TREE_STATS_COLUMNS,
    append_tree_stats,
    export_tree_stats,
    parse_tex_directory,
    parse_tex_file,
    parse_tex_string,
    parse_tex_versions,
)
from src.parser_module.sentence_tokenizer import split_sentences

__all__ = [
    # Core parsing entrypoints
    "parse_tex_versions",
    "export_tree_stats",
    "append_tree_stats",
    "TREE_STATS_COLUMNS",
    "parse_tex_string",
    "parse_tex_file",
    "parse_tex_directory",
    "parse_latex_to_hierarchy",
    # Tree structures
    "HierarchyNode",
    "LaTeXHierarchyTree",
    "HierarchyBuilder",
    # Multi-file gathering
    "find_root_tex_file",
    "gather_compilation_path",
    "merge_tex_sources",
    # Text & sentence processing
    "split_sentences",
    "clean_latex_markup",
    "clean_text_content",
    "normalize_math",
    "strip_latex_comments",
]
