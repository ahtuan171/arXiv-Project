---
title: "Project Milestone 3 — Reference Matching Pipeline"
short_title: "Milestone 3 — Matching"
label: m3-matching
description: "Match the extracted BibTeX entries against the crawled references.json with a full machine learning pipeline, evaluated with Mean Reciprocal Rank."
---

# Objectives

In this milestone, students will apply machine learning techniques to a real matching problem: the citation entries extracted from the parsed text are linked with their external metadata. Concretely, the BibTeX entries produced in Milestone 2 must be matched to the correct arXiv IDs in the `references.json` crawled in Milestone 1 via the Semantic Scholar API [@semantic-scholar-api; @semantic-scholar-platform]. This practice mimics the real-world data science workflow of entity resolution—unstructured text processing, standardization, feature engineering, and matching algorithms—assembled into a complete pipeline.

# Description

Students must perform a matching task between the references extracted and standardized in Milestone 2 and the scraped `references.json` data. The goal is to identify which extracted BibTeX entry corresponds to which arXiv ID (or metadata entry) in the `references.json` file. The full data science pipeline is described below.

## Data Cleaning

Perform any additional text preprocessing required to facilitate the matching process (e.g., lowercasing, stop-word removal, or specific tokenization for author names and titles).

## Data Labelling

Students need to create a ground truth dataset by labelling associations between the extracted BibTeX entries and the entries in `references.json`. The task is to link the converted BibTeX entries from the LaTeX source to the correct arXiv ID found in the `references.json` file. Students are required to:

- Manually label references for at least **5 publications**. The manual ground truth dataset must contain a total of at least **20 labeled reference pairs** (associations between a BibTeX entry and a target arXiv ID).

- Implement automatic matching based on classic tools (such as regex, string similarity heuristics, etc.) to generate labels for at least **10%** of the remaining non-manual data.

The internal format for these labels is up to the students but must be clearly documented in the group's repository and presented in the poster.

## Feature Engineering

Students must first frame the matching task as a specific supervised learning problem (e.g., classification, regression, recommendation, or ranking), which determines the input format (e.g., entry-reference pairs, triplets, or candidate lists). Based on this chosen input format, students must perform feature engineering to construct reasonable features for the model. These features can range from simple string matching scores or embeddings to complex statistical features.

Students must justify the creation of each feature in the poster and the repository documentation, explaining the underlying idea. This insight and idea should be illustrated with appropriate data analysis.

**Note:** In this feature engineering phase, students are allowed to construct features based on hierarchies previously formed in Milestone 2.

## Data Modeling

The target of this problem is to match the entries inside the standardized BibTeX file with associated references inside `references.json` of the same publication.

If there are $m$ BibTeX entries in the merged BibTeX file and $n$ references inside the scraped Semantic Scholar `references.json`, then there are $m \times n$ possible pairs inside that single publication. Thus, there are actually $m \times n$ data points generated from this publication only. Consequently, $m \times n$ labels should be assigned, where each pair has a binary label of yes or no, indicating whether the BibTeX entry from the `.bib` file corresponds to the associated reference in `references.json`.

The data split strategy is as follows:

- **Test Set:** Includes 1 publication from the manually labeled set and 1 publication from the automatically matched set.

- **Validation Set:** Includes 1 publication from the manually labeled set and 1 publication from the automatically matched set.

- **Training Set:** Consists of all remaining publications (the remaining manually labeled ones and the remaining automatically matched ones).

## Model Evaluation

Evaluate the trained models on the **Test set** (comprising the 2 publications defined above). The model should output a ranked list of the **top 5 candidates** from `references.json` for each BibTeX entry. Post-processing of the results is allowed.

The evaluation metric is **Mean Reciprocal Rank (MRR)**.

$$\text{MRR} = \frac{1}{|Q|} \sum_{i=1}^{|Q|} \frac{1}{\text{rank}_i}$$

Where $|Q|$ is the total number of references to match, and $\text{rank}_i$ is the rank position of the correct match within the top 5 candidates. If the correct match is not in the list, the score is zero.

# Requirement

Perform the full pipeline (cleaning, labelling, feature engineering, modeling, evaluation) on the assigned publications. The matching results are published in the `predictions` table of the final Hugging Face dataset (see the Submission Format section). The table contains one row per BibTeX entry of each publication:

| **Column** | **Type** | **Content** |
|:---|:---|:---|
| arxiv_id | string, non-null | Publication the entry belongs to |
| partition | string, dictionary | `train`, `valid`, or `test` |
| citation_key | string, non-null | BibTeX entry name (identifier) |
| groundtruth_arxiv_id | string, nullable | The correct arXiv ID from `references.json`, when known |
| cand_1–cand_5 | string, nullable | The model's ranked top-5 candidates: cand_1 best, cand_5 last |
*Columns of the predictions table*

**Important Note:** Only the publications that are used for the reference matching task (those in the Training, Validation, and Test sets) have rows in this table.

# Submission Format

## Final Data Submission (Hugging Face)

There is **no intermediate data submission** in this project. The final data submission happens in this milestone: the fully processed data is hosted as a public **Hugging Face dataset**—following the hosting instructions given by the Lab Instructors—and the dataset link is pasted via Moodle.

**Storage format: Parquet.** Publish the processed corpus as *sharded Parquet*—the Hub's standard dataset format, which the viewer and the streaming backend read directly—in seven tables with one unified, explicit schema. Keep the columns flat and primitive: one-level string lists (authors, revised dates) are fine, nested objects and serialized JSON blobs are not.

| **Table** | **Columns (types)** | **Built from** |
|:---|:---|:---|
| papers | arxiv_id (string, non-null); title (string); authors (list of strings); submission_date (ISO string); revised_dates (list of strings); venue (string, nullable) | the crawled paper metadata |
| elements | element_id (string, non-null); arxiv_id (string, non-null); kind (string, dictionary); content (string) | the parsed hierarchy, deduplicated across versions |
| element_parents | arxiv_id (string); version (int); element_id (string, non-null); parent_id (string, non-null) | the per-version parent maps of the parsed hierarchy |
| bibtex_entries | arxiv_id (string); citation_key (string, non-null); entry_type (string, dictionary); title (string); authors (list of strings); year (int, nullable); doi (string); journal (string); url (string) | the unified BibTeX entries, fields normalized into these columns |
| paper_references | arxiv_id (string); ref_arxiv_id (string, non-null); ref_title (string); ref_authors (list of strings); ref_submission_date (string); ref_s2_id (string) | the crawled reference metadata, one row per citing–cited pair |
| predictions | The columns are specified in the Requirement section | the matching results |
| sources | arxiv_id (string); version (int); filename (string); content (string) | LaTeX/BibTeX source text per version |
*Tables of the final dataset and their columns*

The dataset repository itself is simple:

    └── <dataset-repo>/
        ├── README.md
        └── data/
            ├── papers/
            ├── elements/
            ├── element_parents/
            ├── bibtex_entries/
            ├── paper_references/
            ├── predictions/
            └── sources/

At the root, a **dataset card** (`README.md` with YAML metadata) tells the Hub how the tables map to files:

**Dataset card metadata (README.md):**

```
---
configs:
- config_name: papers
  data_files:
  - split: train
    path: data/papers/*.parquet
- config_name: elements
  data_files:
  - split: train
    path: data/elements/*.parquet
- ...one config per table...
---
```

The intermediate working formats of Milestones 2 and 3 are up to the group (the suggestions given there are conventional)—this table layout is only the publishing format for the final submission. Avoid custom loading scripts: the filename patterns and the card above are all the Hub needs. **NO late submission is permitted**.

## Source Code

This milestone's code is committed to the same single public GitHub repository used in Milestones 1 and 2, continuing the cumulative history that documents the group's progress. The repository contains only source files such as Python files (`.py` or `.ipynb`)—excluding data files, compiled executables, or any other non-source materials—together with:

```
<group-repo>/
|-- src # folder containing the source code
|   |-- *.{py/ipynb}
|   |-- requirements.txt # To re-produce the environment
|-- README.md # Instructions on environment setup, code execution, feature and label documentation
```

This milestone is submitted via Moodle as a group: the poster file (see below) and the public links to the Hugging Face dataset and to the shared code repository. All the submissions above should be in the official language of your program.

## Poster and Poster Session

The final grading format is a **poster session**. Each group submits **one pptx file containing a single-page poster** in the form factor of **portrait A2** for review. The poster should present the group's selected pieces of work: those representing the *highest quality*, the *most outstanding* results, and the work the group believes makes it *distinct from the others*—for example, the engineered features with their justification and analysis, the model and its MRR results, and notable statistics of the parsing and matching pipeline.

Upon acceptance of the review, the group **prints the poster** and **presents it at the final poster session**.

# Grading Criteria

Your grade is determined upon five main criteria as listed below:

- **ML Pipeline Implementation:** Correct implementation of cleaning, labeling, feature engineering, and modeling steps for reference matching.

- **Evaluation & Analysis:** Proper use of MRR, clear justification of features, and insightful analysis of the results, as presented in the poster and the repository documentation. Note that students using external-knowledge methods such as LLMs or pretrained models shall be judged in separation from the other students.

- **Dataset Schema:** The published Parquet dataset conforms to the table and column specification, ready for streaming.

- **Code Quality & Reproducibility:** Clean, runnable code with clear instructions.

- **Poster & Session:** Quality of the selected work, the single-page portrait A2 poster, and the poster session presentation.
