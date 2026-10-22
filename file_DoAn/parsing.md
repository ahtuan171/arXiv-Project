---
title: "Project Milestone 2 — Hierarchical Parsing and Standardization"
short_title: "Milestone 2 — Parsing"
label: m2-parsing
description: "Transform raw LaTeX sources into a structured hierarchical format with cleaned, normalized, and deduplicated content (hierarchy.json and refs.bib)."
---

# Objectives

In this milestone, students will transition from simple data collection to advanced data processing and analysis. The focus is to transform the unstructured raw data—the LaTeX sources crawled in Milestone 1—into a structured hierarchical format with cleaned, normalized, and deduplicated content. This practice mimics the real-world data science workflow of unstructured text processing and standardization; the machine learning part of this workflow (the reference matching pipeline) follows in Milestone 3 and consumes the outputs produced here.

# Description

Students are required to implement an automatic parser that processes the scraped LaTeX source files from the previous Milestone. The processing pipeline consists of the following components:

## Multi-file Gathering

The content of a LaTeX source is often spread across multiple `.tex` files (e.g., using `\input` or `\include`). Students must programmatically identify the main compilation path and parse only those files that are actually included in the final PDF rendering. Unused or redundant files found in the source directory must be ignored.

## Hierarchy Construction

#### Hierarchy Structure

The goal is to convert the raw LaTeX code into a hierarchical tree structure for each version of each publication.

- The **Document** acts as the root.

- **Chapters** or **Sections** typically comprise the second level.

- Other elements such as **Subsections**, **Paragraphs**, and subsequent logical divisions form the lower levels. Since the exact nesting order depends on the publication's specific formatting, the parser is required to automatically identify the appropriate hierarchical level for each such element.

#### Parsing Notes

To ensure consistency, the parser must adhere to specific rules regarding element granularity and exclusion:

- **Smallest Elements:** The leaf nodes (smallest elements) of the hierarchy must be **Sentences** (separated by dots/periods), **Block Formulas** (mathematical blocks such as contents within `equation` environments or `$$...$$`), and **Figures** (note that **Tables** are also considered a type of Figure for this purpose). The itemized points are considered as branching into deeper levels, meaning that a block of `\begin{itemize}...\end{itemize}` is considered as a higher component with each point being a next-level element.

- **Exclusions:** References sections should **not** be parsed into the hierarchy.

- **Inclusions:** Acknowledgements and Appendices (often unnumbered using `\section*`) **must** be included.

## Standardization and Deduplication

#### Cleanup and Normalization

Students must clean and normalize the data within the hierarchy:

- **LaTeX Cleanup:** Remove unnecessary formatting commands (e.g., `\centering`, `[htpb]`, `\midrule`) that do not contribute to semantic meaning.

- **Math Normalization:** Convert all inline mathematics to a unified format (e.g., `$...$`) and all block mathematics to a unified format (e.g., `\begin{equation}...\end{equation}`).

#### Reference Extraction

For citations defined using `\bibitem` within the `.tex` files, students must convert them into standard BibTeX entries using programmatic tools (e.g., Regular Expressions).

#### Deduplication

Deduplication is required at two levels:

- **Reference Entries:** Duplicated reference entries inside each version or across different versions of a single publication should be properly handled. If different citation keys (names) refer to the same underlying reference content, a single citation key should be chosen, and the `\cite{}` commands of the remaining entries must be renamed to this chosen key. Additionally, the deduplication process should unionize the fields of the duplicate entries rather than selecting one entry while discarding all other information.

- **Full-text Content:** Full-text content deduplication must be applied to the hierarchical elements. If an element's text content matches exactly across different versions (full-text match), it should be represented by a single identifier in the final output, reducing redundancy. Note: full-text matching should be performed *after* cleanup to remove discrepancies in minor details (such as formatting or spacing) that might otherwise cause the code to identify identical texts as different.

# Requirement

Process every publication produced in Milestone 1. For each publication, parse every version's LaTeX source (following the compilation path), build the hierarchical structure, standardize and deduplicate the content, and extract the references into a unified BibTeX file. The milestone thus produces two files per publication:

- **hierarchy.json**: the parsed content and the hierarchical structure, in the format defined below. This single JSON file replaces the raw `tex` folder from the previous Milestone for downstream consumption.

- **refs.bib**: the unified BibTeX entries after deduplication and merging from the hierarchy construction step.

There is **no data submission** in this milestone: the processed corpus stays with the group as the working input of Milestone 3. The following format is a suggested convention—follow it as-is or adapt it to whatever is most convenient for your group; the final data submission on Hugging Face (defined in Milestone 3) can be produced either way.

**Format for hierarchy.json:**

```
{
  "elements": {
    "smallest-element-id": "Cleaned latex content ...",
    "higher-component-id": "Title of the associated content"
  },
  "hierarchy": {
    "1": { // Version 1
      "higher-component-id": "root-document-id", // parent
      "smallest-element-id": "higher-component-id" // parent
    },
    "2": { // Version 2
       "id-string-x": "id-string-y" // parent
    }
  }
}
```

**Note:**

1.  The `elements` field contains elements from **all** versions of the publication. These elements must be deduplicated; meaning if an element's text content matches exactly across versions (full-text match), it should be represented by a single id-string.

2.  The ID elements should contain information about the publication ID (arXiv ID or Semantic Scholar ID) to identify which publication the element belongs to, while the remaining part of the ID should discriminate between elements inside that publication.

# Submission Format

## Source Code

This milestone's code is committed to the same single public GitHub repository used in Milestone 1, continuing the cumulative history that documents the group's progress. The repository contains only source files such as Python files (`.py` or `.ipynb`)—excluding data files, compiled executables, or any other non-source materials—together with:

```
<group-repo>/
|-- src # folder containing the source code
|   |-- *.{py/ipynb}
|   |-- requirements.txt # To re-produce the environment
|-- README.md # Instructions on environment setup and code execution
```

This milestone is submitted via Moodle as a group by pasting the public links to the shared code repository and to the milestone report (see below). All the submissions above should be in the official language of your program. **NO late submission is permitted**.

## Report Requirements (Jupyter Book)

Students must prepare a milestone report compiled as a **Jupyter Book**—the same book repository introduced in Milestone 1, hosted on its GitHub Pages—adding a parsing-and-standardization section to that book rather than creating a new one. The book must present the statistics and analysis of the processed data, and its figures and tables must be *generated during book compilation* (not pasted as static screenshots) for quality and interactivity. The report should specifically cover:

- **Hierarchical Parsing:** Explanation of the logic used to detect file structure, parse the LaTeX hierarchy, and handle multi-file inputs.

- **Standardization:** Details on how data cleaning, math normalization, and reference deduplication were implemented.

- **Statistics:** Relevant statistics such as the success rate of parsing and the element/entry counts before and after deduplication.

The parser's performance should also be illustrated through two main points: running time (such as the end-to-end wall time of the parser and the average time to process one publication) and memory footprint (such as the maximum RAM used and the final output's storage size). The testbed for the final benchmarking is a Google Colab instance running on CPU-only mode. No upper limit on the running time is enforced, while the upper limit on maximum disk usage is given by the Colab instance.

The `README.md` of the repository stays separate: it provides clear instructions on setting up the necessary environment (e.g., Python version, required packages via pip or conda) and running the code with specific configurations.

# Grading Criteria

Your grade is determined upon five main criteria as listed below:

- **Hierarchical Parser:** Accuracy of the parser in detecting structure, handling multi-file sources, and identifying the appropriate hierarchical levels.

- **Standardization & Deduplication:** Correct cleanup, math normalization, reference extraction, and both deduplication levels (reference entries with `\cite` renaming and field unionization; full-text content across versions).

- **Parser's Performance:** How efficient the parser is (running time, memory footprint).

- **Code Quality & Reproducibility:** Clean, runnable code with clear instructions.

- **Report (Jupyter Book):** Figures generated at book compilation, interactivity, and clarity of the hosted book.
