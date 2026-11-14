"""LaTeX to Hierarchical Tree Parser and BibTeX Standardization Module (Milestone 2).

Converts LaTeX source files into structured hierarchical trees and extracts unified BibTeX entries:
  - Multi-file compilation gathering (resolves \\documentclass, \\input, \\include)
  - Math normalization & LaTeX cleanup
  - Tree construction: Document -> Headings -> Lists -> Items -> Leaf Elements (Sentences, Formulas, Figures)
  - Reference extraction & deduplication into unified BibTeX entries
  - Generation of dataset tables: elements, hierarchy, bibtex, and statistics tables
"""

from src.parser_module.bib_parser import (
    BIB_STATS_COLUMNS,
    BIBTEX_COLUMNS,
    BibStats,
    BibtexEntry,
    batch_process_corpus_references,
    clean_latex_text,
    compute_missing_fields_count,
    deduplicate_bibtex_entries,
    export_bib_stats,
    export_bibtex_table,
    extract_bib_file_entries,
    extract_bibitem_entries,
    extract_doi,
    extract_url,
    extract_version_references,
    extract_year,
    find_compilation_files,
    parse_author_list,
    process_and_export_publication_references,
    process_publication_references,
)
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
    # BibTeX parsing and standardization
    "BIB_STATS_COLUMNS",
    "BIBTEX_COLUMNS",
    "BibStats",
    "BibtexEntry",
    "batch_process_corpus_references",
    "clean_latex_text",
    "compute_missing_fields_count",
    "deduplicate_bibtex_entries",
    "export_bib_stats",
    "export_bibtex_table",
    "extract_bib_file_entries",
    "extract_bibitem_entries",
    "extract_doi",
    "extract_url",
    "extract_version_references",
    "extract_year",
    "find_compilation_files",
    "parse_author_list",
    "process_and_export_publication_references",
    "process_publication_references",
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
