"""Hierarchical parser for LaTeX documents into a structured tree of elements.

Builds a hierarchy tree conforming strictly to Milestone 2 requirements:
  - Document acts as the root
  - Headings (Chapters, Sections, Subsections, Paragraphs...) form intermediate levels
  - Acknowledgements and Appendices (including unnumbered \\section*) are INCLUDED
  - Itemized blocks (itemize, enumerate) branch into next-level item elements
  - Leaf nodes are:
      * Sentences (separated by dots, preserving math, commands, abbreviations, decimals, URLs)
      * Block Formulas (normalized equations)
      * Figures & Tables
  - References (\\begin{thebibliography}...\\end{thebibliography}) are EXCLUDED
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from src.parser_module.cleaner import clean_latex_markup, normalize_math, strip_latex_comments
from src.parser_module.sentence_tokenizer import split_sentences

log = logging.getLogger(__name__)

# Heading hierarchy levels
HEADING_LEVELS = {
    "part": 1,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}

_HEADING_RE = re.compile(
    r"\\(?P<cmd>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*\{(?P<title>[^}]+)\}"
)

_ENV_RE = re.compile(
    r"\\begin\{(?P<env>figure\*?|table\*?|equation\*?|align\*?|gather\*?|multline\*?|itemize|enumerate)\}"
)


@dataclass
class HierarchyNode:
    """A node in the parsed LaTeX hierarchy tree."""
    node_id: str
    kind: str  # document, heading, list, item, sentence, formula, figure
    content: str
    level: int = 0
    parent_id: str | None = None
    children: list[HierarchyNode] = field(default_factory=list)

    def is_leaf(self) -> bool:
        return len(self.children) == 0

    def iter_nodes(self) -> Iterator[HierarchyNode]:
        """Depth-first traversal yielding all nodes in the subtree."""
        yield self
        for child in self.children:
            yield from child.iter_nodes()

    def to_dict(self) -> dict[str, Any]:
        """Recursive dictionary representation."""
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "content": self.content,
            "level": self.level,
            "parent_id": self.parent_id,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class LaTeXHierarchyTree:
    """Complete document hierarchy tree for a LaTeX publication or version."""
    root: HierarchyNode
    arxiv_id: str = ""
    version: int = 1

    def flatten_nodes(self) -> list[HierarchyNode]:
        """Return all nodes in the tree as a flat list."""
        return list(self.root.iter_nodes())

    def get_max_depth(self) -> int:
        """Compute the maximum depth of the tree from root (depth 1) to deepest leaf."""
        def _depth(node: HierarchyNode) -> int:
            if not node.children:
                return 1
            return 1 + max(_depth(c) for c in node.children)
        return _depth(self.root)

    def raw_elements_count(self) -> int:
        """Return the total number of raw elements in this tree."""
        return len(self.flatten_nodes())

    def to_dict(self) -> dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "version": self.version,
            "tree": self.root.to_dict(),
        }

    def to_elements_table(self) -> list[dict[str, Any]]:
        """Export flat elements table (node_id, arxiv_id, kind, content)."""
        return [
            {
                "element_id": node.node_id,
                "arxiv_id": self.arxiv_id,
                "kind": node.kind,
                "content": node.content,
            }
            for node in self.flatten_nodes()
        ]

    def to_hierarchy_table(self) -> list[dict[str, Any]]:
        """Export edge table (arxiv_id, version, element_id, parent_id)."""
        edges = []
        for node in self.flatten_nodes():
            if node.parent_id is not None:
                edges.append({
                    "arxiv_id": self.arxiv_id,
                    "version": self.version,
                    "element_id": node.node_id,
                    "parent_id": node.parent_id,
                })
        return edges


def _extract_environment(text: str, start_pos: int, env_name: str) -> tuple[str, int]:
    """Find matching \\begin{env}...\\end{env} taking care of nesting."""
    end_tag = f"\\end{{{env_name}}}"
    begin_tag = f"\\begin{{{env_name}}}"
    depth = 1
    pos = start_pos + len(begin_tag)
    while depth > 0:
        next_begin = text.find(begin_tag, pos)
        next_end = text.find(end_tag, pos)
        if next_end == -1:
            # Unterminated environment; take till end of text
            return text[start_pos:], len(text)
        if next_begin != -1 and next_begin < next_end:
            depth += 1
            pos = next_begin + len(begin_tag)
        else:
            depth -= 1
            if depth == 0:
                full_end = next_end + len(end_tag)
                return text[start_pos:full_end], full_end
            pos = next_end + len(end_tag)
    return text[start_pos:pos], pos


def _clean_caption(env_content: str) -> str:
    """Extract and clean \\caption{...} from figure/table environments."""
    cap_match = re.search(r"\\caption\*?\{([^}]+)\}", env_content)
    if cap_match:
        return clean_latex_markup(cap_match.group(1))
    return clean_latex_markup(env_content)


class HierarchyBuilder:
    """Builds a LaTeXHierarchyTree from consolidated LaTeX source code."""

    def __init__(self, arxiv_id: str = "", version: int = 1):
        self.arxiv_id = arxiv_id
        self.version = version
        prefix = re.sub(r"[._]", "-", arxiv_id) if arxiv_id else "paper"
        self._prefix = f"{prefix}_v{version}"
        self._counter = 0

    def _next_id(self, kind: str) -> str:
        self._counter += 1
        tag = {
            "document": "root",
            "heading": "h",
            "sentence": "s",
            "formula": "eq",
            "figure": "fig",
            "list": "lst",
            "item": "item",
        }.get(kind, "elem")
        return f"{self._prefix}#{tag}{self._counter}"

    def _parse_items_in_list(self, list_content: str, list_node: HierarchyNode):
        """Split an itemize/enumerate environment into item nodes and their sentences."""
        raw_items = re.split(r"\\item\s*(?:\[[^\]]*\])?", list_content)
        for raw in raw_items[1:]:  # skip text preceding first \item
            cleaned = clean_latex_markup(strip_latex_comments(raw))
            if not cleaned:
                continue
            item_node = HierarchyNode(
                node_id=self._next_id("item"),
                kind="item",
                content=cleaned,
                level=list_node.level + 1,
                parent_id=list_node.node_id,
            )
            list_node.children.append(item_node)

            # Leaves inside item
            sentences = split_sentences(cleaned)
            for s in sentences:
                sent_node = HierarchyNode(
                    node_id=self._next_id("sentence"),
                    kind="sentence",
                    content=s,
                    level=item_node.level + 1,
                    parent_id=item_node.node_id,
                )
                item_node.children.append(sent_node)

    def build_tree(self, tex_source: str) -> LaTeXHierarchyTree:
        """Parse raw LaTeX source code into a LaTeXHierarchyTree."""
        # 1. Strip comments
        text = strip_latex_comments(tex_source)

        # 2. Extract content inside \begin{document}...\end{document} if present
        doc_match = re.search(r"\\begin\{document\}(.*?)(?:\\end\{document\}|$)", text, flags=re.DOTALL)
        body = doc_match.group(1) if doc_match else text

        # 3. Exclude references/bibliography section from hierarchy
        body = re.sub(
            r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
            "",
            body,
            flags=re.DOTALL,
        )
        body = re.sub(r"\\bibliography\{[^}]*\}", "", body)

        # 4. Math normalization
        body = normalize_math(body)

        # 5. Root node
        root_id = f"{self._prefix}#root"
        root = HierarchyNode(
            node_id=root_id,
            kind="document",
            content=f"Version {self.version} root",
            level=0,
            parent_id=None,
        )

        # Stack of (level, HierarchyNode)
        stack: list[tuple[int, HierarchyNode]] = [(0, root)]

        def current_parent() -> HierarchyNode:
            return stack[-1][1]

        # Tokenize body into headings, environments, and prose sentences
        pos = 0
        n = len(body)

        while pos < n:
            next_heading = _HEADING_RE.search(body, pos)
            next_env = _ENV_RE.search(body, pos)

            event = None
            event_pos = n

            if next_heading and next_heading.start() < event_pos:
                event = ("heading", next_heading)
                event_pos = next_heading.start()

            if next_env and next_env.start() < event_pos:
                event = ("env", next_env)
                event_pos = next_env.start()

            # Process prose preceding the event
            if event_pos > pos:
                prose = body[pos:event_pos]
                cleaned_prose = clean_latex_markup(prose)
                if cleaned_prose:
                    parent = current_parent()
                    raw_paras = [p.strip() for p in re.split(r"\n\s*\n", cleaned_prose) if p.strip()]
                    for p_text in raw_paras:
                        para_node = HierarchyNode(
                            node_id=self._next_id("paragraph"),
                            kind="paragraph",
                            content=p_text,
                            level=parent.level + 1,
                            parent_id=parent.node_id,
                        )
                        parent.children.append(para_node)
                        sentences = split_sentences(p_text)
                        for s in sentences:
                            para_node.children.append(
                                HierarchyNode(
                                    node_id=self._next_id("sentence"),
                                    kind="sentence",
                                    content=s,
                                    level=para_node.level + 1,
                                    parent_id=para_node.node_id,
                                )
                            )

            if event is None:
                break

            kind, match = event
            if kind == "heading":
                cmd = match.group("cmd")
                title = clean_latex_markup(match.group("title"))
                level = HEADING_LEVELS.get(cmd, 2)

                # Pop stack until top level is strictly smaller than this heading level
                while len(stack) > 1 and stack[-1][0] >= level:
                    stack.pop()

                parent = stack[-1][1]
                heading_node = HierarchyNode(
                    node_id=self._next_id("heading"),
                    kind="heading",
                    content=title,
                    level=level,
                    parent_id=parent.node_id,
                )
                parent.children.append(heading_node)
                stack.append((level, heading_node))

                pos = match.end()

            elif kind == "env":
                env_name = match.group("env")
                env_text, end_pos = _extract_environment(body, match.start(), env_name)
                parent = current_parent()

                if "figure" in env_name or "table" in env_name:
                    # Figures and tables are leaf elements
                    caption = _clean_caption(env_text)
                    fig_node = HierarchyNode(
                        node_id=self._next_id("figure"),
                        kind="figure",
                        content=caption,
                        level=parent.level + 1,
                        parent_id=parent.node_id,
                    )
                    parent.children.append(fig_node)

                elif any(m in env_name for m in ["equation", "align", "gather", "multline"]):
                    # Block formulas are leaf elements
                    formula_node = HierarchyNode(
                        node_id=self._next_id("formula"),
                        kind="formula",
                        content=clean_latex_markup(env_text),
                        level=parent.level + 1,
                        parent_id=parent.node_id,
                    )
                    parent.children.append(formula_node)

                elif env_name in ["itemize", "enumerate"]:
                    # Itemized list branching component
                    list_node = HierarchyNode(
                        node_id=self._next_id("list"),
                        kind="list",
                        content=f"{env_name} block",
                        level=parent.level + 1,
                        parent_id=parent.node_id,
                    )
                    parent.children.append(list_node)
                    self._parse_items_in_list(env_text, list_node)

                pos = end_pos

        return LaTeXHierarchyTree(root=root, arxiv_id=self.arxiv_id, version=self.version)


def parse_latex_to_hierarchy(
    tex_source: str,
    arxiv_id: str = "",
    version: int = 1,
) -> LaTeXHierarchyTree:
    """Convenience function to parse LaTeX source code into a LaTeXHierarchyTree."""
    builder = HierarchyBuilder(arxiv_id=arxiv_id, version=version)
    return builder.build_tree(tex_source)
