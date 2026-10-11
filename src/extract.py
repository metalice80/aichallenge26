"""Safe, in-memory extraction of supported local documents."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 10 * 1024 * 1024
TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".py", ".rst", ".js", ".ts", ".jsx",
    ".tsx", ".json", ".yaml", ".yml", ".toml", ".html", ".css",
    ".sh", ".c", ".h", ".cpp", ".java", ".go", ".rs", ".sql", ".csv",
})
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | {".pdf"}
WORD_PATTERN = re.compile(r"(?<!\w)[^\W_]+(?:[-‑][^\W_]+)*(?!\w)")


class ExtractionError(ValueError):
    """A document cannot safely be extracted or contains no usable text."""

    def __init__(self, message: str, pdf_page_count: int = 0):
        super().__init__(message)
        self.pdf_page_count = pdf_page_count


@dataclass(frozen=True)
class Page:
    number: int
    text: str
    section: str


@dataclass(frozen=True)
class Document:
    source: str
    title: str
    extension: str
    text: str
    pages: tuple[Page, ...] = ()
    pdf_page_count: int = 0
    word_count: int = 0
    estimated_pages: int = 0
    char_count: int = 0
    warnings: tuple[str, ...] = ()


def _safe_source(value: str) -> str:
    normalized = value.replace("\\", "/")
    pieces = PurePosixPath(normalized).parts
    if not pieces or any(part in {"..", "."} for part in pieces) or normalized.startswith("/"):
        raise ExtractionError("Недопустимое имя источника документа.")
    if any(ord(ch) < 32 for ch in normalized):
        raise ExtractionError("Имя документа содержит управляющие символы.")
    return str(PurePosixPath(*pieces))


def _pdf_section(toc: list, page_number: int) -> str:
    parents: list[str] = []
    for level, title, starting_page, *_ in toc:
        if starting_page > page_number:
            continue
        level = max(1, int(level))
        parents = parents[: level - 1]
        parents.append(str(title))
    return " / ".join(parents) if parents else f"Страница {page_number}"


def load_document(name: str, data: bytes, source: str | None = None) -> Document:
    """Extract one uploaded file, never writing its supplied filename to disk."""
    if not isinstance(data, bytes):
        raise ExtractionError("Содержимое файла должно быть байтами.")
    if len(data) > MAX_FILE_BYTES:
        raise ExtractionError("Файл превышает допустимый размер 10 МБ.")
    if not data:
        raise ExtractionError("Файл пуст.")
    title = PurePosixPath(name.replace("\\", "/")).name
    if not title or title in {".", ".."}:
        raise ExtractionError("Недопустимое имя файла.")
    ext = Path(title).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(f"Неподдерживаемый формат: {ext or 'без расширения'}.")
    safe_source = _safe_source(source if source is not None else title)
    if ext != ".pdf":
        if b"\x00" in data:
            raise ExtractionError("Файл похож на бинарный, а не на текстовый.")
        try:
            text = data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise ExtractionError("Текстовый файл должен быть в кодировке UTF-8.") from exc
        if sum(ch in "\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f\x7f" for ch in text) > max(3, len(text) // 100):
            raise ExtractionError("Текст содержит слишком много управляющих символов.")
        if not text.strip():
            raise ExtractionError("В файле нет текста для индексации.")
        words = len(WORD_PATTERN.findall(text))
        if not words:
            raise ExtractionError("В файле нет содержательных слов для индексации.")
        return Document(safe_source, title, ext, text, word_count=words,
                        estimated_pages=math.ceil(words / 500), char_count=len(text))

    pdf_page_count = 0
    try:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ExtractionError("PDF защищён паролем.")
            pdf_page_count = len(pdf)
            toc = pdf.get_toc()
            pages = tuple(Page(number=i + 1, text=page.get_text("text"),
                               section=_pdf_section(toc, i + 1))
                          for i, page in enumerate(pdf))
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Не удалось извлечь текст PDF: {exc}",
                              pdf_page_count=pdf_page_count) from exc
    if not pages or not any(page.text.strip() for page in pages):
        raise ExtractionError("PDF пустой или сканированный: текст не извлекается (OCR не поддерживается).",
                              pdf_page_count=len(pages))
    empty_pages = [str(page.number) for page in pages if not page.text.strip()]
    warnings = (("Страницы без извлечённого текста (возможно, сканы): " + ", ".join(empty_pages),)
                if empty_pages else ())
    text = "\n\n".join(page.text for page in pages)
    return Document(safe_source, title, ext, text, pages=pages,
                    pdf_page_count=len(pages), word_count=len(WORD_PATTERN.findall(text)),
                    char_count=sum(len(page.text) for page in pages), warnings=warnings)


def load_corpus(path: str | Path) -> tuple[list[Document], dict[str, str]]:
    """Read a local example directory. Return individual errors without hiding good files."""
    root = Path(path)
    if not root.is_dir():
        raise ExtractionError(f"Каталог корпуса не найден: {root}")
    documents: list[Document] = []
    errors: dict[str, str] = {}
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink() or not entry.is_file() or entry.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if entry.name.lower() in {"readme.md", "questions.json"}:
            continue
        source = entry.relative_to(root).as_posix()
        try:
            if entry.stat().st_size > MAX_FILE_BYTES:
                raise ExtractionError("Файл превышает допустимый размер 10 МБ.")
            documents.append(load_document(entry.name, entry.read_bytes(), source=source))
        except (ExtractionError, OSError) as exc:
            count = getattr(exc, "pdf_page_count", 0)
            errors[source] = f"{exc} (страниц: {count})" if count else str(exc)
    return documents, errors
