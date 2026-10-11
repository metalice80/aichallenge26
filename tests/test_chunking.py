from __future__ import annotations

import pytest

from src.chunking import chunk_document, chunk_documents
from src.extract import load_document


def assert_ordered_coverage(text: str, chunks) -> None:
    """All non-whitespace input positions are covered, even where windows overlap."""
    last_start = 0
    coverage = bytearray(len(text))
    for chunk in chunks:
        position = text.find(chunk.text, last_start)
        assert position >= 0, chunk.text[:60]
        coverage[position:position + len(chunk.text)] = b"\x01" * len(chunk.text)
        last_start = position
        assert len(chunk.text) <= 1000
        assert chunk.text.strip()
    assert all(covered or ch.isspace() for covered, ch in zip(coverage, text))


def test_fixed_windows_cover_long_words_and_preserve_file_identity():
    content = " ".join(f"слово{i}" for i in range(800))
    first = load_document("one.txt", content.encode())
    second = load_document("two.txt", "отдельный документ".encode())
    chunks = chunk_documents([first, second], "fixed")
    one = [chunk for chunk in chunks if chunk.source == "one.txt"]
    assert len(one) >= 5
    assert all(chunk.strategy == "fixed" and chunk.section == "Файл" and chunk.page is None for chunk in one)
    assert_ordered_coverage(content, one)
    assert chunks[-1].source == "two.txt"
    assert chunks[-1].text == second.text


def test_structural_heading_chain_and_coverage():
    intro = "Предисловие поясняет задачу.\n\n"
    long = " ".join(f"исследование{i}" for i in range(350))
    text = f"{intro}# Глава\nОбзор.\n\n## Подраздел\n{long}\n\n### Деталь\nВыводы эксперимента."
    doc = load_document("manual.md", text.encode())
    chunks = chunk_document(doc, "structural")
    assert_ordered_coverage(text, chunks)
    assert any(c.section == "Глава / Подраздел" for c in chunks)
    assert any(c.section == "Глава / Подраздел / Деталь" for c in chunks)
    assert all(c.source == "manual.md" and c.title == "manual.md" for c in chunks)
    assert len({c.vector_id for c in chunks}) == len(chunks)


def test_structural_python_function_sections():
    doc = load_document("logic.py", b"class Calculator:\n    def add(self, a, b):\n        return a + b\n\ndef noop():\n    return None\n")
    chunks = chunk_document(doc, "structural")
    assert any("Calculator / add" in chunk.section for chunk in chunks)
    assert any(chunk.section == "noop" for chunk in chunks)
    with pytest.raises(ValueError):
        chunk_document(doc, "other")


def test_pdf_page_boundary_and_section():
    from src.extract import Document, Page

    doc = Document("manual.pdf", "manual.pdf", ".pdf", "Первая страница\n\nВторая страница",
                   pages=(Page(1, "Первая страница", "Инструкция"),
                          Page(2, "Вторая страница", "Процедура")))
    for strategy in ("fixed", "structural"):
        chunks = chunk_document(doc, strategy)
        assert [chunk.page for chunk in chunks] == [1, 2]
        assert [chunk.text for chunk in chunks] == ["Первая страница", "Вторая страница"]
        if strategy == "structural":
            assert [chunk.section for chunk in chunks] == ["Инструкция", "Процедура"]
