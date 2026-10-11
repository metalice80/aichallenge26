from __future__ import annotations

import fitz
import pytest

from src.extract import ExtractionError, load_document


def test_text_extraction_and_rejection():
    doc = load_document("notes.md", "# Заголовок\nСодержательный текст".encode(), source="notes.md")
    assert doc.word_count == 3
    assert doc.char_count == len(doc.text)
    for name, data in [("bad.exe", b"hello"), ("bad.txt", b"a\0b"),
                       ("empty.txt", b"  \n"), ("punctuation.md", b"# ! ---"),
                       ("bad.txt", b"\xff")]:
        with pytest.raises(ExtractionError):
            load_document(name, data)
    with pytest.raises(ExtractionError):
        load_document("a.md", b"ok", source="../escape.md")


def test_scanned_pdf_reports_no_ocr_and_mixed_pages_warning():
    pdf = fitz.open()
    pdf.new_page()
    empty = pdf.tobytes()
    pdf.close()
    with pytest.raises(ExtractionError, match="сканированный"):
        load_document("scan.pdf", empty)

    pdf = fitz.open()
    pdf.new_page().insert_text((40, 40), "Page with text")
    pdf.new_page()
    mixed = load_document("mixed.pdf", pdf.tobytes())
    pdf.close()
    assert mixed.pdf_page_count == 2
    assert mixed.pages[0].number == 1
    assert mixed.pages[1].number == 2
    assert "2" in " ".join(mixed.warnings)
