"""Fixed-window and document-aware structural segmentation."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterator

from .extract import Document

FIXED_SIZE = 1000
OVERLAP = 150


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    vector_id: int
    source: str
    title: str
    section: str
    page: int | None
    strategy: str
    text: str
    ordinal: int


@dataclass(frozen=True)
class _Section:
    label: str
    text: str
    page: int | None = None


def _windows(text: str, size: int, overlap: int, structural: bool = False) -> Iterator[str]:
    if not 0 <= overlap < size or size < 1:
        raise ValueError("Размер чанка должен быть больше перекрытия.")
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            if structural and len(text) - end <= overlap:
                # Balance the last two chunks rather than emit a 150-char
                # duplicate carrying just a handful of new characters.
                end = start + (len(text) - start + overlap) // 2
            span = text[start:end]
            if structural and end == start + size:
                candidates = [span.rfind("\n\n"), max(span.rfind(". "), span.rfind(".\n"), span.rfind("! "), span.rfind("? "))]
                break_at = next((position for position in candidates if position >= int(size * .65)), -1)
                if break_at >= 0:
                    end = start + break_at + (2 if span[break_at:break_at + 2] == "\n\n" else 1)
            space = max(text.rfind(" ", start + int((end - start) * .7), end),
                        text.rfind("\n", start + int((end - start) * .7), end))
            if space > start:
                end = space + 1
        part = text[start:end].strip()
        if part:
            yield part
        if end == len(text):
            break
        next_start = max(start + 1, end - overlap)
        # Begin at the beginning of a word, not at an arbitrary character.
        if next_start > 0 and not text[next_start - 1].isspace() and not text[next_start].isspace():
            previous = max(text.rfind(" ", max(start, next_start - 40), next_start),
                           text.rfind("\n", max(start, next_start - 40), next_start))
            if previous > start:
                next_start = previous + 1
            else:
                following = min((p for p in (text.find(" ", next_start, min(end, next_start + 40)),
                                               text.find("\n", next_start, min(end, next_start + 40))) if p >= 0), default=-1)
                if following >= 0:
                    next_start = following + 1
        start = next_start


def _markdown_sections(text: str, rst: bool = False) -> list[_Section]:
    lines = text.splitlines(keepends=True)
    markers: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if not rst else None
        if match:
            markers.append((i, len(match.group(1)), match.group(2).strip()))
        if rst and i + 1 < len(lines) and line.strip() and re.fullmatch(r"([=~`^\-:#*+])\1{2,}", lines[i + 1].strip()):
            if len(lines[i + 1].strip()) >= len(line.strip()):
                symbol = lines[i + 1].strip()[0]
                level = "=~-^\"`:#*+".find(symbol)
                markers.append((i, level + 1 if level >= 0 else 2, line.strip()))
    markers.sort(key=lambda marker: marker[0])
    sections: list[_Section] = []
    levels: list[tuple[int, str]] = []
    previous = 0
    previous_label = "Документ"
    for line_no, depth, title in markers:
        if line_no > previous:
            sections.append(_Section(previous_label, "".join(lines[previous:line_no])))
        levels = [(n, label) for n, label in levels if n < depth]
        levels.append((depth, title))
        previous_label = " / ".join(label for _, label in levels)
        previous = line_no
    if previous < len(lines):
        sections.append(_Section(previous_label, "".join(lines[previous:])))
    return sections


def _code_sections(text: str, extension: str) -> list[_Section]:
    lines = text.splitlines(keepends=True)
    markers: list[tuple[int, int, str]] = []
    if extension == ".py":
        try:
            tree = ast.parse(text)
            def visit(nodes: list[ast.stmt], parents: tuple[str, ...]) -> None:
                for node in nodes:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        lineno = min((decorator.lineno for decorator in node.decorator_list), default=node.lineno)
                        label = parents + (node.name,)
                        markers.append((lineno - 1, len(label), " / ".join(label)))
                        visit(node.body, label)
            visit(tree.body, ())
        except SyntaxError:
            pass
    else:
        for i, line in enumerate(lines):
            match = re.match(r"^\s*(?:(?:export|public|private|static|async)\s+)*(?:class|interface|function|def|fn|func)\s+([\w$]+)", line)
            if match:
                markers.append((i, 1, match.group(1)))
    markers.sort(key=lambda marker: marker[0])
    sections: list[_Section] = []
    prev = 0
    label = "Файл"
    for line_no, _depth, next_label in markers:
        if line_no > prev:
            sections.append(_Section(label, "".join(lines[prev:line_no])))
        label = next_label
        prev = line_no
    if prev < len(lines):
        sections.append(_Section(label, "".join(lines[prev:])))
    return sections


def _paragraph_sections(text: str) -> list[_Section]:
    return [_Section("Файл", text)]


def _sections(document: Document) -> list[_Section]:
    if document.pages:
        return [_Section(page.section, page.text, page.number) for page in document.pages]
    if document.extension in {".md", ".markdown", ".rst"}:
        return _markdown_sections(document.text, rst=document.extension == ".rst")
    if document.extension in {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp", ".h"}:
        return _code_sections(document.text, document.extension)
    return _paragraph_sections(document.text)


def chunk_document(document: Document, strategy: str, size: int = FIXED_SIZE,
                   overlap: int = OVERLAP) -> list[Chunk]:
    """Chunk within file/page boundaries, retaining source, page and heading chain."""
    if strategy not in {"fixed", "structural"}:
        raise ValueError("Неизвестная стратегия разбиения.")
    if not 0 <= overlap < size or size < 1:
        raise ValueError("Размер чанка должен быть больше перекрытия.")
    sections = (_sections(document) if strategy == "structural" else
                [_Section(f"Страница {page.number}", page.text, page.number) for page in document.pages]
                if document.pages else [_Section("Файл", document.text)])
    chunks: list[Chunk] = []
    for section in sections:
        for content in _windows(section.text, size, overlap, structural=strategy == "structural"):
            ordinal = len(chunks)
            digest = sha256("\x00".join((document.source, strategy, str(ordinal), content)).encode("utf-8")).hexdigest()
            vector_id = int(digest[:15], 16)  # positive signed int64, deterministic across builds
            chunks.append(Chunk(digest, vector_id, document.source, document.title,
                                section.label, section.page, strategy, content, ordinal))
    return chunks


def chunk_documents(documents: list[Document], strategy: str, size: int = FIXED_SIZE,
                    overlap: int = OVERLAP) -> list[Chunk]:
    return [chunk for document in documents for chunk in chunk_document(document, strategy, size, overlap)]
