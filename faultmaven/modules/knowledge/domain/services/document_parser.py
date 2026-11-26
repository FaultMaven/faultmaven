"""Document parser for the conversion pipeline.

Extracts plain text from supported file formats (PDF, DOCX, TXT, Markdown, HTML).
Does not interpret or transform content — that is the LLM's job.
"""

import logging
import re
from dataclasses import dataclass, replace
from functools import cache
from io import BytesIO
from pathlib import Path
from typing import Optional

# Not beside ``PdfReader`` below: tests mock ``sys.modules["pypdf"]``, and an
# ``except`` has to name the class pypdf really raises.
from pypdf.errors import LimitReachedError

from faultmaven.modules.knowledge.domain.services.runbook_grammar import (
    mask_html_comments,
)
from faultmaven.utils.exception_name import exception_log_name
from faultmaven.utils.line_endings import normalize_line_endings

logger = logging.getLogger(__name__)


class DocumentRefusal(ValueError):
    """A refusal this parser words itself: a hand-written sentence for the
    caller (an unsupported format, a missing parser library, an undecodable
    text file, an image-only PDF, no text at all). ``extract_document_text``
    passes its text on; anything else an extractor raises is a parser library's
    own exception, whose text can name the file's server path (python-docx:
    ``Package not found at '/tmp/…'``), so only its class is passed on (#836).

    ``code`` is its ``ConversionErrorCode`` value, set where it is raised and
    mapped by ``preparation.prepare``, never read from its wording (#2593). A
    plain string: the preparation worker imports no pydantic."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code

    def __reduce__(self):
        # ``code`` is not in ``args``, so the default reduction would call
        # ``__init__`` without it: pickle and copy would raise TypeError.
        return (_rebuilt_refusal, (type(self), self.args), self.__dict__)


class DocumentTooComplex(DocumentRefusal):
    """pypdf's budgets refused the whole document, or refused pages and no other
    page had text: ``DOCUMENT_TOO_COMPLEX``, never ``FILE_CORRUPT`` (#2153)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="DOCUMENT_TOO_COMPLEX")


class DocumentTooLong(DocumentRefusal):
    """The text passed the caller's ``max_chars``: ``DOCUMENT_TOO_LONG``, worded
    by the caller, which owns the bound (#2937)."""

    def __init__(self, max_chars: int) -> None:
        super().__init__(
            f"The extracted text passed {max_chars:,} characters",
            code="DOCUMENT_TOO_LONG",
        )


class _TextBound:
    """A running lower bound on the length of the text extraction returns,
    to stop reading once it passes ``max_chars``: each part's length less its
    carriage returns, separators not counted, since line endings are
    normalised on the joined text. Every part that adds length is non-blank,
    so a file with no text is never stopped: it answers ``NO_TEXT_LAYER``."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.length = 0

    def add(self, part: str) -> str:
        self.length += len(part) - part.count("\r")
        if self.length > self.max_chars:
            raise DocumentTooLong(self.max_chars)
        return part


def _rebuilt_refusal(cls: type, args: tuple) -> DocumentRefusal:
    """A ``DocumentRefusal`` of ``cls`` with ``args``; ``__reduce__``'s state
    restores ``code``."""
    refusal = cls.__new__(cls)
    refusal.args = args
    return refusal


_TOO_COMPLEX = (
    "This PDF is too complex for the PDF reader's limits, so no text could be "
    "read from it. Export it to text (TXT or Markdown), or split it into smaller "
    "documents, and convert that instead."
)

#: pypdf raises ``LimitReachedError`` for a corrupt file as well as for its
#: budgets, and has no subclass to tell them apart, so corruption is matched by
#: its message. Of the 57 raise sites in pypdf 6.21.4 these are corruption: a
#: cycle or a self-reference ("Detected ...": /Pages, /Parent, /Next, a tree,
#: an outline, an article), an invalid CID width range, and the two recovery
#: limits, reached only once the /Root or a zlib stream has already failed to
#: parse. So are the two "Declared ..." sizes, where the file declares more
#: than it holds: an xref subsection with more entries than the file has room
#: for, and a stream /Length over pypdf's 75 MB stream limit, which no file
#: under that size holds (every upload under the default 10 MB cap). In a
#: larger file FILE_CORRUPT is what main answered anyway. Every other site is
#: a budget: tokens, depth, entries, sizes read and decompressed output.
#: Corruption stays FILE_CORRUPT, for the whole document.
_CORRUPTION_MESSAGES = (
    "Declared ",
    "Detected ",
    "Invalid ",
    "Maximum Root object recovery",
    "Recovery limit reached",
)


def _is_budget(file_path: Path, error: LimitReachedError) -> bool:
    """Whether pypdf refused for one of its budgets, not for corruption."""
    if str(error).startswith(_CORRUPTION_MESSAGES):
        logger.warning(
            "PDF %s: pypdf limit error read as corruption: %s",
            file_path.name,
            exception_log_name(error),
        )
        return False
    return True


#: libxml2's ``XML_ERR_NO_MEMORY``: lxml reports a failed allocation as an
#: ``XMLSyntaxError`` with this code (python-docx parses a DOCX with lxml).
_XML_ERR_NO_MEMORY = 2


def _out_of_memory(error: Exception) -> bool:
    """Whether ``error`` is a parser library's report of a failed allocation."""
    try:
        from lxml.etree import XMLSyntaxError
    except ImportError:
        return False
    return isinstance(error, XMLSyntaxError) and error.code == _XML_ERR_NO_MEMORY


@dataclass(frozen=True)
class ParsedDocument:
    """What extraction read. ``page_count`` is set for a PDF only, and
    ``skipped_pages`` holds the 1-based numbers of the pages pypdf's budgets
    refused: each costs its own text, not the document's."""

    text: str
    page_count: Optional[int] = None
    skipped_pages: tuple[int, ...] = ()


class DocumentParser:
    """Extracts plain text from uploaded documents."""

    SUPPORTED_FORMATS = {
        "application/pdf": "pdf",
        "application/msword": "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "text/plain": "txt",
        "text/markdown": "markdown",
        "text/html": "html",
    }

    SUPPORTED_EXTENSIONS = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".doc": "docx",
        ".txt": "txt",
        ".md": "markdown",
        ".markdown": "markdown",
        ".html": "html",
        ".htm": "html",
    }

    def extract_document_text(
        self, file_path: Path, content_type: Optional[str] = None, *, max_chars: int
    ) -> ParsedDocument:
        """Extract plain text from a document file.

        Args:
            file_path: Path to the document file.
            content_type: MIME type hint (used if extension is ambiguous).
            max_chars: The longest text returned. A PDF, DOCX or HTML file
                stops reading parts once its text is sure to pass it.

        Returns:
            The extracted text, with a PDF's page count and refused pages.

        Raises:
            DocumentTooComplex: pypdf refused the whole document for its limits.
            DocumentTooLong: the text, if any, is longer than ``max_chars``.
            MemoryError: past the preparation worker's memory limit (#2622).
            DocumentRefusal: the parser's own refusal, with its code.
            ValueError: a parser library's exception, named by its class.
        """
        fmt = self._detect_format(file_path, content_type)

        extractors = {
            "pdf": self._extract_pdf,
            "docx": self._extract_docx,
            "txt": self._extract_txt,
            "markdown": self._extract_markdown,
            "html": self._extract_html,
        }

        extractor = extractors.get(fmt)
        if not extractor:
            raise DocumentRefusal(
                f"Unsupported format: {fmt}", code="UNSUPPORTED_FORMAT"
            )

        try:
            extracted = extractor(file_path, max_chars)
        except (DocumentTooComplex, DocumentTooLong, MemoryError):
            raise
        except DocumentRefusal as e:
            raise DocumentRefusal(
                f"Failed to parse {file_path.name}: {e}", code=e.code
            ) from e
        except Exception as e:
            if _out_of_memory(e):
                raise MemoryError(f"parsing {file_path.name}") from e
            # The log line and the caller get the class, never the library's
            # text: the preprocessor turns this message into ``/convert``'s 422
            # detail.
            logger.warning(
                "document extraction failed for %s: %s",
                file_path,
                exception_log_name(e),
            )
            raise ValueError(
                f"Failed to parse {file_path.name} ({type(e).__name__})"
            ) from e

        if isinstance(extracted, str):
            extracted = ParsedDocument(extracted)
        if not extracted.text.strip():
            raise DocumentRefusal(
                f"No extractable text found in {file_path.name}. "
                "The file may be image-only (scanned PDF) or empty.",
                code="NO_TEXT_LAYER",
            )

        # ONE normalisation for all five extractors (#1403). The txt/markdown/html
        # readers come back through ``Path.read_text``, which is already
        # universal-newlines, so this is the identity for them. The other two are
        # not, and both were measured smuggling a bare CR out of a file that had
        # none of its own:
        #
        # * pypdf does NOT implement PDF 32000 7.3.4.2 ("an unescaped EOL in a
        #   literal string shall be treated as 0Ah"). ``read_string_from_stream``
        #   handles only the ESCAPED EOL; an unescaped ``\r`` falls through to
        #   ``txt.append(ord(tok))`` and survives as 0x0D. Measured on 6.16.2:
        #   ``(alpha\rbravo)`` decodes to ``'alpha\rbravo'``.
        # * BeautifulSoup decodes the ``&#13;`` entity to a literal ``\r``.
        #
        # A CR here is not cosmetic: ``cleanup_text``'s first rule is
        # ``(?m)^(?:Page \d+...)$``, and page-footer stripping is precisely what
        # the PDF path needs. Under CR the ``$`` never anchors, so page numbers
        # and uncollapsed blank-line runs ride into the conversion prompt.
        text = normalize_line_endings(extracted.text)
        if len(text) > max_chars:
            raise DocumentTooLong(max_chars)
        return replace(extracted, text=text)

    def _detect_format(self, file_path: Path, content_type: Optional[str]) -> str:
        """Determine format from MIME type or file extension."""
        if content_type and content_type in self.SUPPORTED_FORMATS:
            return self.SUPPORTED_FORMATS[content_type]

        ext = file_path.suffix.lower()
        if ext in self.SUPPORTED_EXTENSIONS:
            return self.SUPPORTED_EXTENSIONS[ext]

        raise DocumentRefusal(
            f"Unsupported file type: {ext or content_type}. "
            f"Supported: PDF, DOCX, TXT, Markdown, HTML",
            code="UNSUPPORTED_FORMAT",
        )

    def _extract_pdf(self, file_path: Path, max_chars: int) -> ParsedDocument:
        """Extract text from PDF using pypdf, page by page.

        A page pypdf's budgets refuse costs that page, and its number is kept. A
        budget opening the document or its page tree costs the document, as do
        refused pages with no other page's text: checked before "no extractable
        text", so a refusal is never read as an image-only PDF. Corruption that
        pypdf reports as a limit stays the whole document's (FILE_CORRUPT).
        """
        try:
            from pypdf import PdfReader
        except ImportError:
            raise DocumentRefusal(
                "pypdf is required for PDF parsing", code="FILE_CORRUPT"
            )

        try:
            reader = PdfReader(str(file_path))
            page_count = len(reader.pages)
        except LimitReachedError as e:
            if not _is_budget(file_path, e):
                raise
            logger.warning(
                "PDF %s: pypdf refused the document: %s",
                file_path.name,
                exception_log_name(e),
            )
            raise DocumentTooComplex(_TOO_COMPLEX) from e
        pages, skipped, bounds, bound = [], [], set(), _TextBound(max_chars)
        for index in range(page_count):
            try:
                text = reader.pages[index].extract_text()
            except LimitReachedError as e:
                if not _is_budget(file_path, e):
                    raise
                skipped.append(index + 1)
                bounds.add(exception_log_name(e))
                continue
            if text and text.strip():
                pages.append(bound.add(text))

        if skipped:
            logger.warning(
                "PDF %s: pypdf refused %d of %d pages, among them %s: %s",
                file_path.name,
                len(skipped),
                page_count,
                skipped[:10],
                "; ".join(sorted(bounds)[:3]),
            )
        if not pages and skipped:
            raise DocumentTooComplex(_TOO_COMPLEX)
        if not pages:
            raise DocumentRefusal(
                "No extractable text in PDF. The file may be scanned/image-only.",
                code="NO_TEXT_LAYER",
            )

        return ParsedDocument("\n\n".join(pages), page_count, tuple(skipped))

    def _extract_docx(self, file_path: Path, max_chars: int) -> str:
        """Extract text from DOCX preserving structure.

        A paragraph's style is python-docx's own lookup of its ``w:pStyle`` id,
        made once per distinct id: ``Paragraph.style`` scans the document's
        styles on every read, about 4 ms a paragraph (#2597). An id that is
        unknown or not a paragraph style's costs two scans, its lookup and the
        library's fallback to the default, once per distinct id; the worker's
        deadline bounds a document of many such ids (#2622).
        """
        try:
            from docx import Document
            from docx.enum.style import WD_STYLE_TYPE
        except ImportError:
            raise DocumentRefusal(
                "python-docx is required for DOCX parsing", code="FILE_CORRUPT"
            )

        doc = Document(str(file_path))

        @cache
        def style_name_of(style_id: Optional[str]) -> Optional[str]:
            style = doc.part.get_style(style_id, WD_STYLE_TYPE.PARAGRAPH)
            return style.name if style is not None else None

        parts, bound = [], _TextBound(max_chars)

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            # Convert heading styles to markdown headings
            style_name = style_name_of(para._p.style)
            if style_name:
                style_name = style_name.lower()
                if style_name.startswith("heading"):
                    try:
                        level = int(style_name.replace("heading", "").strip())
                        text = "#" * level + " " + text
                    except ValueError:
                        pass

            parts.append(bound.add(text))

        # Extract tables as markdown tables
        for table in doc.tables:
            table_text = self._docx_table_to_markdown(table)
            if table_text:
                parts.append(bound.add(table_text))

        return "\n\n".join(parts)

    def _docx_table_to_markdown(self, table) -> str:
        """Convert a DOCX table to markdown format."""
        rows = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            rows.append("| " + " | ".join(cells) + " |")

        if len(rows) < 1:
            return ""

        # Add header separator after first row
        header = rows[0]
        separator = "| " + " | ".join(["---"] * len(table.rows[0].cells)) + " |"
        return "\n".join([header, separator] + rows[1:])

    def _read_text_with_fallback(self, file_path: Path) -> str:
        """Read text file with encoding fallback: UTF-8 → Latin-1."""
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            pass
        try:
            return file_path.read_text(encoding="latin-1")
        except UnicodeDecodeError:
            raise DocumentRefusal(
                "Cannot decode file — it is not valid UTF-8 or Latin-1 text. "
                "Please re-save the file as UTF-8 and try again.",
                code="ENCODING_ERROR",
            )

    def _extract_txt(self, file_path: Path, max_chars: int) -> str:
        """Read plain text file. ``extract_document_text`` bounds its length:
        the read is its whole cost."""
        return self._read_text_with_fallback(file_path)

    def _extract_markdown(self, file_path: Path, max_chars: int) -> str:
        """Read markdown file, blanking embedded HTML comments. Bounded as TXT
        is, after the blanking, so a file of comments has no text.

        Routed through ``runbook_grammar.mask_html_comments`` — "THE single
        decision about what a comment is; every consumer routes here" — rather
        than a local ``<!--.*?-->`` sweep, which was wrong twice over and
        measurably disarmed the already-a-runbook gate this text feeds (#1375):

        * **Fence-blind.** A runbook that shows ``<!--`` inside a fenced example
          had everything from that token to the next ``-->`` anywhere in the
          document deleted. Measured on a document ``RunbookValidator`` PASSES:
          ``## Causes`` vanished, so ``detect_existing_runbook`` answered False
          for a genuine runbook and it was converted and fragmented.
        * **Splicing.** ``re.sub`` to the empty string joins the lines either
          side, which can stop a heading being a heading. The shared mask blanks
          in place, so every offset and line number survives.
        """
        return mask_html_comments(self._read_text_with_fallback(file_path))

    def _extract_html(self, file_path: Path, max_chars: int) -> str:
        """Extract text from HTML, preserving structural elements."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise DocumentRefusal(
                "beautifulsoup4 is required for HTML parsing", code="FILE_CORRUPT"
            )

        raw_html = self._read_text_with_fallback(file_path)
        soup = BeautifulSoup(raw_html, "html.parser")

        # Remove non-content elements
        for tag in soup.find_all(
            ["nav", "footer", "aside", "script", "style", "header"]
        ):
            tag.decompose()

        parts, bound = [], _TextBound(max_chars)
        for element in soup.find_all(
            [
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "p",
                "pre",
                "code",
                "ul",
                "ol",
                "table",
                "li",
            ]
        ):
            if element.name and element.name.startswith("h"):
                level = int(element.name[1])
                parts.append(
                    bound.add("#" * level + " " + element.get_text(strip=True))
                )
            elif element.name in ("pre", "code"):
                code_text = element.get_text()
                if code_text.strip():
                    parts.append(bound.add(f"```\n{code_text}\n```"))
            elif element.name == "li":
                parts.append(bound.add(f"- {element.get_text(strip=True)}"))
            elif element.name == "table":
                parts.append(bound.add(self._html_table_to_markdown(element)))
            elif element.name == "p":
                text = element.get_text(strip=True)
                if text:
                    parts.append(bound.add(text))

        return "\n\n".join(parts)

    def _html_table_to_markdown(self, table_element) -> str:
        """Convert an HTML table to markdown format."""
        rows = []
        for tr in table_element.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")

        if len(rows) < 1:
            return ""

        header = rows[0]
        num_cols = len(table_element.find("tr").find_all(["td", "th"]))
        separator = "| " + " | ".join(["---"] * num_cols) + " |"
        return "\n".join([header, separator] + rows[1:])
