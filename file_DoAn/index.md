---
title: arXiv Project — Problem Statements
short_title: Project Statements
label: arxiv-project
description: Group project for the Introduction to Data Science course — data crawling, hierarchical parsing and standardization, and the reference matching pipeline.
---

## Logistics

The project is a **group work**. Each milestone is submitted as a group via Moodle:

- **Shared code repository** — one public code repository per group serves the whole project: each milestone's code is committed to it cumulatively, so the commit history documents the group's progress. Milestones 1 and 2 paste its link via Moodle together with the report link; Milestone 3 submits it together with the poster and the dataset.
- **Jupyter Book (Milestones 1 and 2)** — the milestone reports form one public **Jupyter Book**, hosted on its own separate repository's GitHub Pages: Milestone 1's report is the book's first section and Milestone 2 adds the next section. Report figures and tables must be generated during book compilation.
- **Final data submission** — there is no intermediate data submission. The fully processed data is hosted as a **Hugging Face dataset** of sharded Parquet tables, following the Milestone 3 schema and the hosting instructions given by the Lab Instructors, and the dataset link is submitted via Moodle.
- **Final grading** — a **poster session**. Each group submits one pptx file containing a single-page poster in the form factor of **portrait A2** for review; upon acceptance, the group prints the poster and presents it at the poster session.

## Milestones

- **{ref}`m1-crawling`** — build the raw corpus: discover the assigned arXiv papers in the range provided by the Lab Instructors, crawl every version of each publication (full-text LaTeX sources, metadata, and reference metadata), and analyze the scraped corpus.
- **{ref}`m2-parsing`** — structure the corpus: parse each version into a hierarchical tree, standardize the content, convert `\bibitem` references into BibTeX, deduplicate across versions, and analyze the processed data.
- **{ref}`m3-matching`** — run the machine learning pipeline: clean, label, engineer features, model, and evaluate a reference-matching system scored with Mean Reciprocal Rank (MRR).

Milestone-specific requirements and submission formats are detailed in each milestone page.

## Regulations

Ensure your code is well-documented with clear comments. You can use online documents for reference, but you must provide complete citations. You are free to discuss your topics with classmates, but your work must be implemented and interpreted according to your own understanding.

**Any milestone will be graded zero if there is any dishonest behaviour detected.**
