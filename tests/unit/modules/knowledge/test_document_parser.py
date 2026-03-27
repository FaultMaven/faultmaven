"""Unit tests for DocumentParser.

Tests text extraction from PDF, DOCX, TXT, Markdown, and HTML formats.
External libraries (pypdf, python-docx, beautifulsoup4) are mocked.
"""

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
)


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
        result = parser.parse(txt_file)

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
        result = parser.parse(txt_file)

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
            parser.parse(txt_file)

    @pytest.mark.unit
    def test_extract_txt_whitespace_only_raises(self, parser, tmp_path):
        """Whitespace-only TXT file raises ValueError."""
        # Arrange
        txt_file = tmp_path / "blank.txt"
        txt_file.write_text("   \n\n\t  ", encoding="utf-8")

        # Act / Assert
        with pytest.raises(ValueError, match="No extractable text"):
            parser.parse(txt_file)


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
        result = parser.parse(md_file)

        # Assert
        assert result == content

    @pytest.mark.unit
    def test_extract_markdown_strips_single_line_comment(self, parser, tmp_path):
        """Single-line HTML comments are removed."""
        # Arrange
        md_file = tmp_path / "doc.md"
        md_file.write_text("Before <!-- hidden --> After", encoding="utf-8")

        # Act
        result = parser.parse(md_file)

        # Assert
        assert "hidden" not in result
        assert "Before" in result
        assert "After" in result

    @pytest.mark.unit
    def test_extract_markdown_strips_multiline_comment(self, parser, tmp_path):
        """Multi-line HTML comments are removed."""
        # Arrange
        content = textwrap.dedent(
            """\
            # Title

            <!-- TODO:
            This is a draft note
            spanning multiple lines
            -->

            Real content here."""
        )
        md_file = tmp_path / "draft.md"
        md_file.write_text(content, encoding="utf-8")

        # Act
        result = parser.parse(md_file)

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
        result = parser.parse(md_file)

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
        result = parser.parse(html_file)

        # Assert
        assert "# Title" in result
        assert "## Subtitle" in result
        assert "### Section" in result

    @pytest.mark.unit
    def test_extract_html_strips_nav_footer_script(self, parser, tmp_path):
        """nav, footer, script, style, aside, header tags are removed."""
        # Arrange
        html = textwrap.dedent(
            """\
            <html><body>
            <nav><a href="/">Home</a></nav>
            <header>Site Header</header>
            <aside>Sidebar</aside>
            <p>Main content</p>
            <script>alert('xss')</script>
            <style>body { color: red; }</style>
            <footer>Copyright 2026</footer>
            </body></html>"""
        )
        html_file = tmp_path / "full.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.parse(html_file)

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
        result = parser.parse(html_file)

        # Assert
        assert "```" in result
        assert "def hello():" in result

    @pytest.mark.unit
    def test_extract_html_preserves_tables(self, parser, tmp_path):
        """HTML tables are converted to markdown table syntax."""
        # Arrange
        html = textwrap.dedent(
            """\
            <html><body>
            <table>
            <tr><th>Name</th><th>Value</th></tr>
            <tr><td>CPU</td><td>85%</td></tr>
            <tr><td>Memory</td><td>72%</td></tr>
            </table>
            </body></html>"""
        )
        html_file = tmp_path / "table.html"
        html_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.parse(html_file)

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
        result = parser.parse(html_file)

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
        result = parser.parse(html_file)

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
            parser.parse(html_file)

    @pytest.mark.unit
    def test_extract_html_htm_extension(self, parser, tmp_path):
        """The .htm extension is supported."""
        # Arrange
        html = "<html><body><p>Content</p></body></html>"
        htm_file = tmp_path / "page.htm"
        htm_file.write_text(html, encoding="utf-8")

        # Act
        result = parser.parse(htm_file)

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
                result = parser.parse(pdf_file, content_type="application/pdf")

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
            result = parser.parse(pdf_file, content_type="application/pdf")

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
            result = parser.parse(pdf_file, content_type="application/pdf")

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
                parser.parse(pdf_file, content_type="application/pdf")

    @pytest.mark.unit
    def test_extract_pdf_import_error_raises(self, parser, tmp_path):
        """Missing pypdf raises ValueError with install hint."""
        # Arrange
        pdf_file = tmp_path / "needs_lib.pdf"
        pdf_file.write_bytes(b"fake pdf content")

        with patch.dict("sys.modules", {"pypdf": None}):
            with pytest.raises(ValueError, match="pypdf is required"):
                parser.parse(pdf_file, content_type="application/pdf")


# ---------------------------------------------------------------------------
# DOCX extraction (mocked)
# ---------------------------------------------------------------------------


class TestDocxExtraction:
    """Tests for DOCX extraction with mocked python-docx."""

    def _make_paragraph(self, text, style_name=None):
        """Helper to create a mock paragraph."""
        para = MagicMock()
        para.text = text
        style = MagicMock()
        style.name = style_name
        para.style = style
        return para

    def _make_table(self, rows_data):
        """Helper to create a mock table with rows of cell text."""
        table = MagicMock()
        rows = []
        for row_cells in rows_data:
            row = MagicMock()
            cells = [MagicMock(text=cell) for cell in row_cells]
            row.cells = cells
            rows.append(row)
        table.rows = rows
        return table

    @pytest.mark.unit
    def test_extract_docx_paragraphs(self, parser, tmp_path):
        """Paragraphs are joined with double newlines."""
        # Arrange
        docx_file = tmp_path / "doc.docx"
        docx_file.write_bytes(b"fake docx")

        mock_doc = MagicMock()
        mock_doc.paragraphs = [
            self._make_paragraph("First paragraph", "Normal"),
            self._make_paragraph("Second paragraph", "Normal"),
        ]
        mock_doc.tables = []

        with patch.dict(
            "sys.modules",
            {"docx": MagicMock(Document=MagicMock(return_value=mock_doc))},
        ):
            result = parser.parse(docx_file, content_type="application/msword")

        # Assert
        assert "First paragraph" in result
        assert "Second paragraph" in result

    @pytest.mark.unit
    def test_extract_docx_headings_to_markdown(self, parser, tmp_path):
        """DOCX heading styles are converted to markdown headings."""
        # Arrange
        docx_file = tmp_path / "headings.docx"
        docx_file.write_bytes(b"fake docx")

        mock_doc = MagicMock()
        mock_doc.paragraphs = [
            self._make_paragraph("Title", "Heading 1"),
            self._make_paragraph("Subtitle", "Heading 2"),
            self._make_paragraph("Section", "Heading 3"),
            self._make_paragraph("Body text", "Normal"),
        ]
        mock_doc.tables = []

        with patch.dict(
            "sys.modules",
            {"docx": MagicMock(Document=MagicMock(return_value=mock_doc))},
        ):
            result = parser.parse(docx_file, content_type="application/msword")

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
        docx_file.write_bytes(b"fake docx")

        mock_doc = MagicMock()
        mock_doc.paragraphs = [
            self._make_paragraph("Real text", "Normal"),
            self._make_paragraph("", "Normal"),
            self._make_paragraph("   ", "Normal"),
            self._make_paragraph("More text", "Normal"),
        ]
        mock_doc.tables = []

        with patch.dict(
            "sys.modules",
            {"docx": MagicMock(Document=MagicMock(return_value=mock_doc))},
        ):
            result = parser.parse(docx_file, content_type="application/msword")

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
        docx_file.write_bytes(b"fake docx")

        mock_doc = MagicMock()
        mock_doc.paragraphs = [
            self._make_paragraph("Before table", "Normal"),
        ]
        mock_doc.tables = [
            self._make_table(
                [
                    ["Host", "Status"],
                    ["srv1", "OK"],
                    ["srv2", "DOWN"],
                ]
            )
        ]

        with patch.dict(
            "sys.modules",
            {"docx": MagicMock(Document=MagicMock(return_value=mock_doc))},
        ):
            result = parser.parse(docx_file, content_type="application/msword")

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
                parser.parse(docx_file, content_type="application/msword")


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
            parser.parse(csv_file)

    @pytest.mark.unit
    def test_unsupported_mime_and_extension_raises(self, parser, tmp_path):
        """Unknown MIME type + unsupported extension raises ValueError."""
        # Arrange
        bin_file = tmp_path / "data.bin"
        bin_file.write_bytes(b"\x00\x01\x02")

        # Act / Assert
        with pytest.raises(ValueError, match="Unsupported file type"):
            parser.parse(bin_file, content_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Parse method integration (format detection -> extraction -> validation)
# ---------------------------------------------------------------------------


class TestParseIntegration:
    """Tests for the parse() method end-to-end flow."""

    @pytest.mark.unit
    def test_parse_with_content_type_hint(self, parser, tmp_path):
        """Content type hint is used for format detection."""
        # A .log file that's actually plain text
        log_file = tmp_path / "app.log"
        log_file.write_text("2026-03-22 ERROR: disk full", encoding="utf-8")

        result = parser.parse(log_file, content_type="text/plain")
        assert "disk full" in result

    @pytest.mark.unit
    def test_parse_infers_format_from_extension(self, parser, tmp_path):
        """Without content type, format is inferred from extension."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Some notes", encoding="utf-8")

        result = parser.parse(txt_file)
        assert result == "Some notes"

    @pytest.mark.unit
    def test_parse_extraction_error_wrapped(self, parser, tmp_path):
        """Extraction errors are wrapped in ValueError with filename."""
        # Create a .txt file that will fail to read (simulate by making it a dir)
        broken = tmp_path / "broken.md"
        broken.mkdir()

        with pytest.raises(ValueError, match="Failed to parse broken.md"):
            parser.parse(broken)
