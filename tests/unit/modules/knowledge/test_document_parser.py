"""Unit tests for DocumentParser.

Tests text extraction from PDF, DOCX, TXT, Markdown, and HTML formats.
pypdf and beautifulsoup4 are mocked; DOCX files are built with python-docx.
"""

import ast
import base64
import copy
import pickle
import sys
import textwrap
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    DocumentRefusal,
    DocumentTooComplex,
)
from faultmaven.modules.knowledge.domain.services.preparation import MAX_TEXT_CHARS


@pytest.fixture
def parser():
    """Create a DocumentParser instance."""
    return DocumentParser()


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


class TestFormatDetection:
    """Tests for _detect_format: MIME type and file extension resolution."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "mime_type, expected_format",
        [
            ("application/pdf", "pdf"),
            ("application/msword", "docx"),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "docx",
            ),
            ("text/plain", "txt"),
            ("text/markdown", "markdown"),
            ("text/html", "html"),
        ],
    )
    def test_detect_format_from_mime_type(self, parser, mime_type, expected_format):
        """MIME type takes precedence over extension."""
        # Arrange
        file_path = Path("/tmp/document.unknown")

        # Act
        result = parser._detect_format(file_path, content_type=mime_type)

        # Assert
        assert result == expected_format

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "extension, expected_format",
        [
            (".pdf", "pdf"),
            (".docx", "docx"),
            (".doc", "docx"),
            (".txt", "txt"),
            (".md", "markdown"),
            (".markdown", "markdown"),
            (".html", "html"),
            (".htm", "html"),
        ],
    )
    def test_detect_format_from_extension(self, parser, extension, expected_format):
        """Falls back to extension when no MIME type is provided."""
        # Arrange
        file_path = Path(f"/tmp/document{extension}")

        # Act
        result = parser._detect_format(file_path, content_type=None)

        # Assert
        assert result == expected_format

    @pytest.mark.unit
    def test_detect_format_extension_case_insensitive(self, parser):
        """Extension matching should be case-insensitive."""
        result = parser._detect_format(Path("/tmp/FILE.PDF"), content_type=None)
        assert result == "pdf"

    @pytest.mark.unit
    def test_detect_format_mime_type_overrides_extension(self, parser):
        """When both MIME type and extension are available, MIME type wins."""
        # A .txt file served with HTML content type
        result = parser._detect_format(Path("/tmp/page.txt"), content_type="text/html")
        assert result == "html"

    @pytest.mark.unit
    def test_detect_format_unsupported_raises(self, parser):
        """Unsupported extension with no MIME type raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported file type"):
            parser._detect_format(Path("/tmp/data.csv"), content_type=None)

    @pytest.mark.unit
    def test_detect_format_unsupported_mime_falls_back_to_extension(self, parser):
        """Unknown MIME type falls through to extension check."""
        result = parser._detect_format(
            Path("/tmp/readme.md"), content_type="application/octet-stream"
        )
        assert result == "markdown"

    @pytest.mark.unit
    def test_detect_format_no_extension_no_mime_raises(self, parser):
        """File with no extension and no MIME type raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported file type"):
            parser._detect_format(Path("/tmp/noextension"), content_type=None)


# ---------------------------------------------------------------------------
# TXT extraction
# ---------------------------------------------------------------------------


class TestTxtExtraction:
    """Tests for plain text file extraction."""

    @pytest.mark.unit
    def test_extract_txt_reads_file(self, parser, tmp_path):
        """TXT extraction returns file contents verbatim."""
        # Arrange
        content = "Line one\nLine two\nLine three"
        txt_file = tmp_path / "sample.txt"
        txt_file.write_text(content, encoding="utf-8")

        # Act
        result = parser.extract_document_text(txt_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert result == content

    @pytest.mark.unit
    def test_extract_txt_utf8_content(self, parser, tmp_path):
        """TXT extraction handles UTF-8 characters."""
        # Arrange
        content = "Diagnostics: CPU temps 85\u00b0C, \u2713 passed, \u26a0 warning"
        txt_file = tmp_path / "unicode.txt"
        txt_file.write_text(content, encoding="utf-8")

        # Act
        result = parser.extract_document_text(txt_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert result == content

    @pytest.mark.unit
    def test_extract_txt_empty_raises(self, parser, tmp_path):
        """Empty TXT file raises ValueError."""
        # Arrange
        txt_file = tmp_path / "empty.txt"
        txt_file.write_text("", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="No extractable text"):
            parser.extract_document_text(txt_file, max_chars=MAX_TEXT_CHARS)

    @pytest.mark.unit
    def test_extract_txt_whitespace_only_raises(self, parser, tmp_path):
        """Whitespace-only TXT file raises ValueError."""
        # Arrange
        txt_file = tmp_path / "blank.txt"
        txt_file.write_text("   \n\n\t  ", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="No extractable text"):
            parser.extract_document_text(txt_file, max_chars=MAX_TEXT_CHARS)


# ---------------------------------------------------------------------------
# Markdown extraction
# ---------------------------------------------------------------------------


class TestMarkdownExtraction:
    """Tests for Markdown file extraction with HTML comment stripping."""

    @pytest.mark.unit
    def test_extract_markdown_preserves_content(self, parser, tmp_path):
        """Markdown content is returned as-is (minus HTML comments)."""
        # Arrange
        content = "# Heading\n\nSome paragraph text.\n\n- item 1\n- item 2"
        md_file = tmp_path / "readme.md"
        md_file.write_text(content, encoding="utf-8")

        # Act
        result = parser.extract_document_text(md_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert result == content

    @pytest.mark.unit
    def test_extract_markdown_strips_single_line_comment(self, parser, tmp_path):
        """Single-line HTML comments are removed."""
        # Arrange
        md_file = tmp_path / "doc.md"
        md_file.write_text("Before <!-- hidden --> After", encoding="utf-8")

        # Act
        result = parser.extract_document_text(md_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "hidden" not in result
        assert "Before" in result
        assert "After" in result

    @pytest.mark.unit
    def test_extract_markdown_strips_multiline_comment(self, parser, tmp_path):
        """Multi-line HTML comments are removed."""
        # Arrange
        content = textwrap.dedent("""\
            # Title

            <!-- TODO:
            This is a draft note
            spanning multiple lines
            -->

            Real content here.""")
        md_file = tmp_path / "draft.md"
        md_file.write_text(content, encoding="utf-8")

        # Act
        result = parser.extract_document_text(md_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "TODO" not in result
        assert "draft note" not in result
        assert "Real content here." in result
        assert "# Title" in result

    @pytest.mark.unit
    def test_extract_markdown_strips_multiple_comments(self, parser, tmp_path):
        """Multiple HTML comments throughout the file are all removed."""
        # Arrange
        md_file = tmp_path / "multi.md"
        md_file.write_text("A <!-- c1 --> B <!-- c2 --> C", encoding="utf-8")

        # Act
        result = parser.extract_document_text(md_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "c1" not in result
        assert "c2" not in result
        assert "A" in result
        assert "B" in result
        assert "C" in result


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------


class TestHtmlExtraction:
    """Tests for HTML extraction: tag stripping, heading conversion, code blocks, tables."""

    @pytest.mark.unit
    def test_extract_html_headings_to_markdown(self, parser, tmp_path):
        """h1-h6 tags are converted to markdown heading syntax."""
        # Arrange
        html = (
            "<html><body><h1>Title</h1><h2>Subtitle</h2><h3>Section</h3></body></html>"
        )
        html_file = tmp_path / "page.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "# Title" in result
        assert "## Subtitle" in result
        assert "### Section" in result

    @pytest.mark.unit
    def test_extract_html_strips_nav_footer_script(self, parser, tmp_path):
        """nav, footer, script, style, aside, header tags are removed."""
        # Arrange
        html = textwrap.dedent("""\
            <html><body>
            <nav><a href="/">Home</a></nav>
            <header>Site Header</header>
            <aside>Sidebar</aside>
            <p>Main content</p>
            <script>alert('xss')</script>
            <style>body { color: red; }</style>
            <footer>Copyright 2026</footer>
            </body></html>""")
        html_file = tmp_path / "full.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "Main content" in result
        assert "Home" not in result
        assert "Site Header" not in result
        assert "Sidebar" not in result
        assert "alert" not in result
        assert "color: red" not in result
        assert "Copyright" not in result

    @pytest.mark.unit
    def test_extract_html_preserves_code_blocks(self, parser, tmp_path):
        """pre/code tags are wrapped in markdown code fences."""
        # Arrange
        html = "<html><body><pre>def hello():\n    print('hi')</pre></body></html>"
        html_file = tmp_path / "code.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "```" in result
        assert "def hello():" in result

    @pytest.mark.unit
    def test_extract_html_preserves_tables(self, parser, tmp_path):
        """HTML tables are converted to markdown table syntax."""
        # Arrange
        html = textwrap.dedent("""\
            <html><body>
            <table>
            <tr><th>Name</th><th>Value</th></tr>
            <tr><td>CPU</td><td>85%</td></tr>
            <tr><td>Memory</td><td>72%</td></tr>
            </table>
            </body></html>""")
        html_file = tmp_path / "table.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "| Name | Value |" in result
        assert "| --- | --- |" in result
        assert "| CPU | 85% |" in result
        assert "| Memory | 72% |" in result

    @pytest.mark.unit
    def test_extract_html_paragraphs(self, parser, tmp_path):
        """Paragraph text is extracted."""
        # Arrange
        html = (
            "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
        )
        html_file = tmp_path / "para.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "First paragraph." in result
        assert "Second paragraph." in result

    @pytest.mark.unit
    def test_extract_html_list_items(self, parser, tmp_path):
        """List items are converted to markdown bullet points."""
        # Arrange
        html = "<html><body><ul><li>Alpha</li><li>Beta</li></ul></body></html>"
        html_file = tmp_path / "list.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "- Alpha" in result
        assert "- Beta" in result

    @pytest.mark.unit
    def test_extract_html_empty_body_raises(self, parser, tmp_path):
        """HTML with no extractable text raises ValueError."""
        # Arrange
        html = "<html><body><nav>Menu</nav><script>code()</script></body></html>"
        html_file = tmp_path / "empty_body.html"
        html_file.write_text(html, encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="No extractable text"):
            parser.extract_document_text(html_file, max_chars=MAX_TEXT_CHARS)

    @pytest.mark.unit
    def test_extract_html_htm_extension(self, parser, tmp_path):
        """The .htm extension is supported."""
        # Arrange
        html = "<html><body><p>Content</p></body></html>"
        htm_file = tmp_path / "page.htm"
        htm_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.extract_document_text(htm_file, max_chars=MAX_TEXT_CHARS).text

        # Assert
        assert "Content" in result


# ---------------------------------------------------------------------------
# PDF extraction (mocked)
# ---------------------------------------------------------------------------


class TestPdfExtraction:
    """Tests for PDF extraction with mocked pypdf."""

    @pytest.mark.unit
    def test_extract_pdf_single_page(self, parser, tmp_path):
        """Single-page PDF returns page text."""
        # Arrange
        pdf_file = tmp_path / "single.pdf"
        pdf_file.write_bytes(b"fake pdf content")

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page 1 content"
        mock_reader = MagicMock()
        mock_reader.pages = [mock_page]

        with patch(
            "faultmaven.modules.knowledge.domain.services.document_parser.PdfReader",
            create=True,
        ) as mock_cls:
            # Patch the import inside _extract_pdf
            with patch.dict(
                "sys.modules",
                {"pypdf": MagicMock(PdfReader=MagicMock(return_value=mock_reader))},
            ):
                result = parser.extract_document_text(
                    pdf_file, content_type="application/pdf", max_chars=MAX_TEXT_CHARS
                ).text

        # Assert
        assert result == "Page 1 content"

    @pytest.mark.unit
    def test_extract_pdf_multiple_pages(self, parser, tmp_path):
        """Multi-page PDF joins pages with double newlines."""
        # Arrange
        pdf_file = tmp_path / "multi.pdf"
        pdf_file.write_bytes(b"fake pdf content")

        page1 = MagicMock()
        page1.extract_text.return_value = "Chapter 1"
        page2 = MagicMock()
        page2.extract_text.return_value = "Chapter 2"
        page3 = MagicMock()
        page3.extract_text.return_value = "Chapter 3"

        mock_reader = MagicMock()
        mock_reader.pages = [page1, page2, page3]

        with patch.dict(
            "sys.modules",
            {"pypdf": MagicMock(PdfReader=MagicMock(return_value=mock_reader))},
        ):
            result = parser.extract_document_text(
                pdf_file, content_type="application/pdf", max_chars=MAX_TEXT_CHARS
            ).text

        # Assert
        assert result == "Chapter 1\n\nChapter 2\n\nChapter 3"

    @pytest.mark.unit
    def test_extract_pdf_skips_empty_pages(self, parser, tmp_path):
        """Pages with no text are excluded from output."""
        # Arrange
        pdf_file = tmp_path / "sparse.pdf"
        pdf_file.write_bytes(b"fake pdf content")

        page1 = MagicMock()
        page1.extract_text.return_value = "Has text"
        page2 = MagicMock()
        page2.extract_text.return_value = ""  # empty page
        page3 = MagicMock()
        page3.extract_text.return_value = None  # None page

        mock_reader = MagicMock()
        mock_reader.pages = [page1, page2, page3]

        with patch.dict(
            "sys.modules",
            {"pypdf": MagicMock(PdfReader=MagicMock(return_value=mock_reader))},
        ):
            result = parser.extract_document_text(
                pdf_file, content_type="application/pdf", max_chars=MAX_TEXT_CHARS
            ).text

        # Assert
        assert result == "Has text"

    @pytest.mark.unit
    def test_extract_pdf_all_empty_pages_raises(self, parser, tmp_path):
        """PDF where all pages have no text raises ValueError."""
        # Arrange
        pdf_file = tmp_path / "scanned.pdf"
        pdf_file.write_bytes(b"fake pdf content")

        page1 = MagicMock()
        page1.extract_text.return_value = ""
        page2 = MagicMock()
        page2.extract_text.return_value = None

        mock_reader = MagicMock()
        mock_reader.pages = [page1, page2]

        with patch.dict(
            "sys.modules",
            {"pypdf": MagicMock(PdfReader=MagicMock(return_value=mock_reader))},
        ):
            with pytest.raises(ValueError, match="No extractable text"):
                parser.extract_document_text(
                    pdf_file, content_type="application/pdf", max_chars=MAX_TEXT_CHARS
                )

    @pytest.mark.unit
    def test_extract_pdf_import_error_raises(self, parser, tmp_path):
        """Missing pypdf raises ValueError with install hint."""
        # Arrange
        pdf_file = tmp_path / "needs_lib.pdf"
        pdf_file.write_bytes(b"fake pdf content")

        with patch.dict("sys.modules", {"pypdf": None}):
            with pytest.raises(ValueError, match="pypdf is required"):
                parser.extract_document_text(
                    pdf_file, content_type="application/pdf", max_chars=MAX_TEXT_CHARS
                )


# ---------------------------------------------------------------------------
# DOCX extraction
# ---------------------------------------------------------------------------


class TestDocxExtraction:
    """Tests for DOCX extraction over files python-docx builds. The parser
    reads a paragraph's style id from its XML (#2597), so a mocked
    ``para.style`` would be a mock it never reads."""

    def _write_docx(self, path, paragraphs, tables=()):
        """Write ``(text, style)`` paragraphs, then tables of rows of cell text."""
        from docx import Document

        doc = Document()
        for text, style in paragraphs:
            doc.add_paragraph(text, style=style)
        for rows in tables:
            table = doc.add_table(rows=len(rows), cols=len(rows[0]))
            for row, cells in zip(table.rows, rows):
                for cell, text in zip(row.cells, cells):
                    cell.text = text
        doc.save(str(path))

    @pytest.mark.unit
    def test_extract_docx_paragraphs(self, parser, tmp_path):
        """Paragraphs are joined with double newlines."""
        # Arrange
        docx_file = tmp_path / "doc.docx"
        self._write_docx(
            docx_file, [("First paragraph", "Normal"), ("Second paragraph", "Normal")]
        )

        result = parser.extract_document_text(
            docx_file, content_type="application/msword", max_chars=MAX_TEXT_CHARS
        ).text

        # Assert
        assert "First paragraph" in result
        assert "Second paragraph" in result

    @pytest.mark.unit
    def test_extract_docx_headings_to_markdown(self, parser, tmp_path):
        """DOCX heading styles are converted to markdown headings."""
        # Arrange
        docx_file = tmp_path / "headings.docx"
        self._write_docx(
            docx_file,
            [
                ("Title", "Heading 1"),
                ("Subtitle", "Heading 2"),
                ("Section", "Heading 3"),
                ("Body text", "Normal"),
            ],
        )

        result = parser.extract_document_text(
            docx_file, content_type="application/msword", max_chars=MAX_TEXT_CHARS
        ).text

        # Assert
        assert "# Title" in result
        assert "## Subtitle" in result
        assert "### Section" in result
        assert "Body text" in result

    @pytest.mark.unit
    def test_extract_docx_skips_empty_paragraphs(self, parser, tmp_path):
        """Empty paragraphs are excluded."""
        # Arrange
        docx_file = tmp_path / "sparse.docx"
        self._write_docx(
            docx_file,
            [
                ("Real text", "Normal"),
                ("", "Normal"),
                ("   ", "Normal"),
                ("More text", "Normal"),
            ],
        )

        result = parser.extract_document_text(
            docx_file, content_type="application/msword", max_chars=MAX_TEXT_CHARS
        ).text

        # Assert
        assert "Real text" in result
        assert "More text" in result
        parts = result.split("\n\n")
        # Only the two non-empty paragraphs
        assert len(parts) == 2

    @pytest.mark.unit
    def test_extract_docx_tables_to_markdown(self, parser, tmp_path):
        """DOCX tables are converted to markdown table syntax."""
        # Arrange
        docx_file = tmp_path / "table.docx"
        self._write_docx(
            docx_file,
            [("Before table", "Normal")],
            [[["Host", "Status"], ["srv1", "OK"], ["srv2", "DOWN"]]],
        )

        result = parser.extract_document_text(
            docx_file, content_type="application/msword", max_chars=MAX_TEXT_CHARS
        ).text

        # Assert
        assert "| Host | Status |" in result
        assert "| --- | --- |" in result
        assert "| srv1 | OK |" in result
        assert "| srv2 | DOWN |" in result

    @pytest.mark.unit
    def test_extract_docx_import_error_raises(self, parser, tmp_path):
        """Missing python-docx raises ValueError with install hint."""
        # Arrange
        docx_file = tmp_path / "needs_lib.docx"
        docx_file.write_bytes(b"fake docx")

        with patch.dict("sys.modules", {"docx": None}):
            with pytest.raises(ValueError, match="python-docx is required"):
                parser.extract_document_text(
                    docx_file,
                    content_type="application/msword",
                    max_chars=MAX_TEXT_CHARS,
                )


# ---------------------------------------------------------------------------
# Unsupported format
# ---------------------------------------------------------------------------


class TestUnsupportedFormat:
    """Tests for unsupported file formats."""

    @pytest.mark.unit
    def test_unsupported_extension_raises(self, parser, tmp_path):
        """File with unsupported extension raises ValueError."""
        # Arrange
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("a,b,c", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="Unsupported file type"):
            parser.extract_document_text(csv_file, max_chars=MAX_TEXT_CHARS)

    @pytest.mark.unit
    def test_unsupported_mime_and_extension_raises(self, parser, tmp_path):
        """Unknown MIME type + unsupported extension raises ValueError."""
        # Arrange
        bin_file = tmp_path / "data.bin"
        bin_file.write_bytes(b"\x00\x01\x02")

        # Act / Assert
        with pytest.raises(ValueError, match="Unsupported file type"):
            parser.extract_document_text(
                bin_file,
                content_type="application/octet-stream",
                max_chars=MAX_TEXT_CHARS,
            )


# ---------------------------------------------------------------------------
# extract_document_text integration (format detection -> extraction -> validation)
# ---------------------------------------------------------------------------


class TestParseIntegration:
    """Tests for the extract_document_text() end-to-end flow."""

    @pytest.mark.unit
    def test_parse_with_content_type_hint(self, parser, tmp_path):
        """Content type hint is used for format detection."""
        # A .log file that's actually plain text
        log_file = tmp_path / "app.log"
        log_file.write_text("2026-03-22 ERROR: disk full", encoding="utf-8")

        result = parser.extract_document_text(
            log_file, content_type="text/plain", max_chars=MAX_TEXT_CHARS
        ).text
        assert "disk full" in result

    @pytest.mark.unit
    def test_parse_infers_format_from_extension(self, parser, tmp_path):
        """Without content type, format is inferred from extension."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Some notes", encoding="utf-8")

        result = parser.extract_document_text(txt_file, max_chars=MAX_TEXT_CHARS).text
        assert result == "Some notes"

    @pytest.mark.unit
    def test_parse_extraction_error_wrapped(self, parser, tmp_path):
        """Extraction errors are wrapped in ValueError with filename."""
        # Create a .txt file that will fail to read (simulate by making it a dir)
        broken = tmp_path / "broken.md"
        broken.mkdir()

        with pytest.raises(ValueError, match="Failed to parse broken.md"):
            parser.extract_document_text(broken, max_chars=MAX_TEXT_CHARS)


# ---------------------------------------------------------------------------
# How a parse failure is classified (#836)
# ---------------------------------------------------------------------------


def _pdf_with_filter(name: bytes) -> bytes:
    """A minimal one-page PDF whose content stream claims the filter ``name``."""
    stream = b"BT /F1 12 Tf 72 720 Td (hello) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /%s >>\nstream\n" % (len(stream), name)
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out


class TestParseFailureClassification:
    """``preparation.prepare`` classifies a parser library's own exception by
    words in its message ("encoding"/"decode", "unsupported"). Since #836 that
    exception contributes only its CLASS to the message, because its text can
    name the file's server path. The parser's own refusals
    (``DocumentRefusal``) keep their sentence and carry their code (#2593)."""

    @pytest.mark.unit
    def test_a_foreign_extractor_error_is_file_corrupt(self, tmp_path):
        """A real PDF with an unknown stream filter: pypdf raises
        ``NotImplementedError("Unsupported filter /Foo")``. Its text used to
        reach the classifier, and "Unsupported" made it UNSUPPORTED_FORMAT
        (415, whose copy says to upload a PDF, which this is). Only the class
        reaches it now, so it is FILE_CORRUPT (422)."""
        from faultmaven.modules.knowledge.domain.services.preparation import prepare

        pdf = tmp_path / "odd-filter.pdf"
        pdf.write_bytes(_pdf_with_filter(b"Foo"))

        result = prepare(pdf, "application/pdf", lambda stage: None)

        assert result["refused"] == "FILE_CORRUPT"
        assert result["reason"].endswith("(NotImplementedError)")

    @pytest.mark.unit
    def test_a_library_decode_error_is_still_an_encoding_error(
        self, tmp_path, monkeypatch
    ):
        """The class name is all that reaches the classifier, and it still
        decides: a library's ``UnicodeDecodeError`` is ENCODING_ERROR."""
        from faultmaven.modules.knowledge.domain.services.preparation import prepare

        def fail(self, file_path, max_chars):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "at /srv/tmp/notes")

        monkeypatch.setattr(DocumentParser, "_extract_html", fail)
        page = tmp_path / "page.html"
        page.write_text("<p>text</p>", encoding="utf-8")

        result = prepare(page, "text/html", lambda stage: None)

        assert result == {
            "refused": "ENCODING_ERROR",
            "reason": "Failed to parse page.html (UnicodeDecodeError)",
        }


# ---------------------------------------------------------------------------
# A parser refusal's code comes from its raise site, never its wording (#2593)
# ---------------------------------------------------------------------------


#: A 1x1 PNG.
_ONE_PIXEL_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _blank_page_pdf(tmp_path: Path) -> tuple[Path, str]:
    """A real PDF of one blank page: no text layer, as a scanned image."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    path = tmp_path / "scan.pdf"
    with path.open("wb") as out:
        writer.write(out)
    return path, "application/pdf"


def _image_only_docx(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A real DOCX whose one paragraph holds a picture and no text."""
    from docx import Document

    document = Document()
    document.add_picture(BytesIO(base64.b64decode(_ONE_PIXEL_PNG)))
    path = tmp_path / "scan.docx"
    document.save(str(path))
    return path, _DOCX


def _undecodable_txt(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """Through the real fallback reader, over a file neither codec decodes:
    unreachable from real bytes, because Latin-1 decodes any byte."""

    class _Undecodable:
        name = "notes.txt"

        def read_text(self, encoding):
            raise UnicodeDecodeError(encoding, b"\xff", 0, 1, "invalid")

    monkeypatch.setattr(
        DocumentParser,
        "_extract_txt",
        lambda self, file_path, max_chars: self._read_text_with_fallback(
            _Undecodable()
        ),
    )
    path = tmp_path / "notes.txt"
    path.write_text("some text that will not be read", encoding="utf-8")
    return path, "text/plain"


def _unknown_format(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A format with no extractor: unreachable while every detected format
    has one."""
    monkeypatch.setattr(DocumentParser, "_detect_format", lambda self, *a: "rtf")
    path = tmp_path / "notes.txt"
    path.write_text("text", encoding="utf-8")
    return path, "text/plain"


def _without(module: str, name: str, header: bytes, content_type: str):
    """A file whose parser library cannot be imported."""

    def build(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
        monkeypatch.setitem(sys.modules, module, None)
        path = tmp_path / name
        path.write_bytes(header + b" body")
        return path, content_type

    return build


def _file(name: str, body: bytes, content_type: str):
    def build(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
        path = tmp_path / name
        path.write_bytes(body)
        return path, content_type

    return build


#: Every parser-worded refusal: how it is reached, its code, its detail. Each
#: code and detail but NO_TEXT_LAYER's is main's (a document with no text to
#: read answered FILE_CORRUPT); the detail is unchanged for every one (#2593).
_REFUSALS = [
    pytest.param(
        lambda tmp_path, monkeypatch: _blank_page_pdf(tmp_path),
        "NO_TEXT_LAYER",
        "Failed to parse scan.pdf: No extractable text in PDF. The file may be "
        "scanned/image-only.",
        id="image-only-pdf",
    ),
    pytest.param(
        _undecodable_txt,
        "ENCODING_ERROR",
        "Failed to parse notes.txt: Cannot decode file — it is not valid UTF-8 "
        "or Latin-1 text. Please re-save the file as UTF-8 and try again.",
        id="undecodable-text",
    ),
    pytest.param(
        _without("pypdf", "a.pdf", b"%PDF-1.4", "application/pdf"),
        "FILE_CORRUPT",
        "Failed to parse a.pdf: pypdf is required for PDF parsing",
        id="no-pypdf",
    ),
    pytest.param(
        _without("docx", "a.docx", b"PK\x03\x04", "application/msword"),
        "FILE_CORRUPT",
        "Failed to parse a.docx: python-docx is required for DOCX parsing",
        id="no-python-docx",
    ),
    pytest.param(
        _without("bs4", "a.html", b"<p>x</p>", "text/html"),
        "FILE_CORRUPT",
        "Failed to parse a.html: beautifulsoup4 is required for HTML parsing",
        id="no-beautifulsoup4",
    ),
    pytest.param(
        _file("blank.txt", b"  \n\n ", "text/plain"),
        "NO_TEXT_LAYER",
        "No extractable text found in blank.txt. The file may be image-only "
        "(scanned PDF) or empty.",
        id="whitespace-only-text",
    ),
    pytest.param(
        _image_only_docx,
        "NO_TEXT_LAYER",
        "No extractable text found in scan.docx. The file may be image-only "
        "(scanned PDF) or empty.",
        id="image-only-docx",
    ),
    pytest.param(
        _file("notes.rtf", b"text", "application/rtf"),
        "UNSUPPORTED_FORMAT",
        "Unsupported file type: .rtf. Supported: PDF, DOCX, TXT, Markdown, HTML",
        id="unsupported-file-type",
    ),
    pytest.param(
        _unknown_format,
        "UNSUPPORTED_FORMAT",
        "Unsupported format: rtf",
        id="unsupported-format",
    ),
]


class TestRefusalCodeByType:
    """A ``DocumentRefusal`` carries its code from its raise site: wording it
    differently cannot move it, and a document with no text to read is
    NO_TEXT_LAYER."""

    @pytest.mark.unit
    @pytest.mark.parametrize("build, code, detail", _REFUSALS)
    def test_each_refusal_answers_its_code_and_detail(
        self, tmp_path, monkeypatch, build, code, detail
    ):
        from faultmaven.modules.knowledge.domain.services.preparation import prepare

        path, content_type = build(tmp_path, monkeypatch)

        result = prepare(path, content_type, lambda stage: None)

        assert result == {"refused": code, "reason": detail}

    @pytest.mark.unit
    @pytest.mark.parametrize("build, code, detail", _REFUSALS)
    def test_a_reworded_refusal_keeps_its_code(
        self, tmp_path, monkeypatch, build, code, detail
    ):
        """Each sentence reworded to hold the words the library-exception
        classifier reads as another code."""
        from faultmaven.modules.knowledge.domain.services import document_parser
        from faultmaven.modules.knowledge.domain.services.preparation import prepare

        misleading = (
            "This file is unsupported."
            if code == "ENCODING_ERROR"
            else "This file cannot be decoded."
        )

        class Reworded(document_parser.DocumentRefusal):
            def __init__(self, message: str, *, code: str) -> None:
                super().__init__(misleading, code=code)

        monkeypatch.setattr(document_parser, "DocumentRefusal", Reworded)
        path, content_type = build(tmp_path, monkeypatch)

        result = prepare(path, content_type, lambda stage: None)

        assert result == {"refused": code, "reason": misleading}


@pytest.mark.unit
@pytest.mark.parametrize(
    "refusal",
    [
        DocumentRefusal("Cannot decode file.", code="ENCODING_ERROR"),
        DocumentTooComplex("Too complex."),
    ],
    ids=["refusal", "too-complex"],
)
def test_a_refusal_survives_pickle_and_copy(refusal):
    """``code`` is not in ``args``: the default reduction would call
    ``__init__`` without it."""
    for twin in (pickle.loads(pickle.dumps(refusal)), copy.copy(refusal)):
        assert type(twin) is type(refusal)
        assert str(twin) == str(refusal)
        assert twin.code == refusal.code


def _codes_raised_by_the_parser() -> set[str]:
    """Every string literal passed as ``code=`` in ``document_parser.py``."""
    from faultmaven.modules.knowledge.domain.services import document_parser

    tree = ast.parse(Path(document_parser.__file__).read_text(encoding="utf-8"))
    return {
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "code" and isinstance(keyword.value, ast.Constant)
    }


def _status_mapped_codes() -> set[str]:
    """The ``ConversionErrorCode`` members ``/convert``'s ``status_map`` names."""
    from faultmaven.modules.knowledge.api import conversion_routes

    tree = ast.parse(Path(conversion_routes.__file__).read_text(encoding="utf-8"))
    (status_map,) = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and [getattr(target, "id", None) for target in node.targets] == ["status_map"]
    ]
    return {key.attr for key in status_map.keys}


@pytest.mark.unit
def test_every_code_the_parser_raises_is_a_mapped_conversion_error_code():
    """The worker sends a plain string the parent turns into a
    ``ConversionErrorCode`` (``document_preprocessor``): a misspelt code would be
    a 500, and an unmapped one a 422 by default rather than by decision."""
    from faultmaven.modules.knowledge.domain.models.conversion import (
        ConversionErrorCode,
    )

    raised = _codes_raised_by_the_parser()

    assert {"NO_TEXT_LAYER", "DOCUMENT_TOO_COMPLEX"} <= raised  # the scan reads
    assert raised <= {code.value for code in ConversionErrorCode}
    assert raised <= _status_mapped_codes()
