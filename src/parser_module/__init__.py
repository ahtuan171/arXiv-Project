"""Parsing and standardization module for Milestone 2."""

from src.parser_module.bib_parser import (
    BIBTEX_COLUMNS,
    BibtexEntry,
    clean_latex_text,
    deduplicate_bibtex_entries,
    export_bibtex_table,
    extract_bib_file_entries,
    extract_bibitem_entries,
    extract_doi,
    extract_url,
    extract_version_references,
    extract_year,
    find_compilation_files,
    parse_author_list,
    process_publication_references,
)

__all__ = [
    "BIBTEX_COLUMNS",
    "BibtexEntry",
    "clean_latex_text",
    "deduplicate_bibtex_entries",
    "export_bibtex_table",
    "extract_bib_file_entries",
    "extract_bibitem_entries",
    "extract_doi",
    "extract_url",
    "extract_version_references",
    "extract_year",
    "find_compilation_files",
    "parse_author_list",
    "process_publication_references",
]
