"""文档解析：把上传原件转成可切分的纯文本，并显式标注解析质量。

设计约束（对应 `docs/13` 阶段 2）：

- 只承认真正解析成功的格式；未知/二进制格式必须显式拒绝，不得按 UTF-8 硬解码后
  把乱码写入知识库（原实现 `content.decode("utf-8", errors="ignore")` 会静默产生垃圾块）。
- 解析结果必须带状态：`PARSED`（完整）、`PARTIAL`（部分内容缺失，如扫描页）、
  `FAILED`（完全无可用文本）。状态写入 `DocumentVersion.parse_status`，让失败可见。
- 扫描件不在此模块做 OCR；无文本页通过 `pages_without_text` 暴露给上层，由 OCR
  环节（独立服务）补齐，避免把"未识别"伪装成"已解析"。
- 不引入新依赖：XLSX 用 zipfile + ElementTree，HTML 用 html.parser，CSV 用 csv。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree

from ..errors import ApiError
from ..text_utils import bound_text

PARSED = "PARSED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"

MAX_TEXT_CHARS = 200_000
MAX_SHEET_ROWS = 5_000
MAX_CSV_ROWS = 20_000

PDF_EXTENSIONS = frozenset({".pdf"})
DOCX_EXTENSIONS = frozenset({".docx"})
XLSX_EXTENSIONS = frozenset({".xlsx", ".xlsm"})
CSV_EXTENSIONS = frozenset({".csv", ".tsv"})
HTML_EXTENSIONS = frozenset({".html", ".htm"})
TEXT_EXTENSIONS = frozenset(
    {".txt", ".text", ".log", ".md", ".markdown", ".rst", ".json", ".yaml", ".yml"}
)

# 明确不支持但常见于司法卷宗的格式：显式拒绝并提示，避免乱码入库。
REJECTED_EXTENSIONS: dict[str, str] = {
    ".doc": "旧版 Word(.doc) 暂不支持，请另存为 .docx",
    ".xls": "旧版 Excel(.xls) 暂不支持，请另存为 .xlsx",
    ".ppt": "旧版 PowerPoint(.ppt) 暂不支持，请另存为 .pptx",
    ".pptx": "PPT 不在知识库解析范围内，请先导出文本或 PDF",
    ".rtf": "RTF 暂不支持，请另存为 .docx 或 .txt",
    ".wps": "WPS 专有格式暂不支持，请另存为 .docx",
    ".ofd": "OFD 版式文件暂不支持，请提供 PDF 或文本",
    ".png": "图片需先经 OCR 服务识别后入库",
    ".jpg": "图片需先经 OCR 服务识别后入库",
    ".jpeg": "图片需先经 OCR 服务识别后入库",
    ".tif": "图片需先经 OCR 服务识别后入库",
    ".tiff": "图片需先经 OCR 服务识别后入库",
    ".bmp": "图片需先经 OCR 服务识别后入库",
}

SUPPORTED_EXTENSIONS = (
    PDF_EXTENSIONS
    | DOCX_EXTENSIONS
    | XLSX_EXTENSIONS
    | CSV_EXTENSIONS
    | HTML_EXTENSIONS
    | TEXT_EXTENSIONS
)


@dataclass
class ParsedDocument:
    text: str
    parser: str
    status: str = PARSED
    page_count: int | None = None
    pages: list[str] | None = None
    warning: str | None = None
    pages_without_text: list[int] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "status": self.status,
            "page_count": self.page_count,
            "pages_without_text": list(self.pages_without_text),
            "warning": self.warning,
            "truncated": self.truncated,
        }


def extension_of(filename: str) -> str:
    match = re.search(r"(\.[A-Za-z0-9]{1,8})$", filename.strip())
    return match.group(1).lower() if match else ""


def _decode_text(content: bytes) -> tuple[str, str]:
    """按常见中文编码依次尝试，避免 GBK 卷宗变成乱码。"""

    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace"), "latin-1"


def _apply_budget(text: str) -> tuple[str, bool]:
    return bound_text(text, MAX_TEXT_CHARS)


class _HTMLTextExtractor(HTMLParser):
    SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "template", "svg"})
    BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "td",
            "th",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "section",
            "article",
            "header",
            "footer",
            "blockquote",
            "pre",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _parse_pdf(content: bytes) -> ParsedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ApiError(
                "DOCUMENT_ENCRYPTED",
                "PDF 已加密，无法解析；请提供已解密的原件",
                422,
            )
        pages = [page.extract_text() or "" for page in reader.pages]
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 解析失败需转成明确业务错误
        raise ApiError(
            "DOCUMENT_PARSE_FAILED",
            "PDF 解析失败，文件可能损坏",
            422,
            {"parser": "pypdf", "error": str(exc)[:240]},
        ) from exc

    missing = [index for index, page in enumerate(pages, 1) if not page.strip()]
    text = "\n\n".join(pages)
    status = PARTIAL if missing else PARSED
    warning = None
    if missing:
        warning = (
            f"{len(missing)}/{len(pages)} 页未提取到文本，可能是扫描件或空白页，"
            "需 OCR 或人工确认"
        )
    return ParsedDocument(
        text=text,
        parser="pypdf",
        status=status,
        page_count=len(pages),
        pages=pages,
        warning=warning,
        pages_without_text=missing,
    )


def _parse_docx(content: bytes) -> ParsedDocument:
    from docx import Document as DocxDocument

    try:
        document = DocxDocument(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            "DOCUMENT_PARSE_FAILED",
            "DOCX 解析失败，文件可能损坏或不是真正的 Word 文档",
            422,
            {"parser": "python-docx", "error": str(exc)[:240]},
        ) from exc

    parts = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return ParsedDocument(text="\n\n".join(parts), parser="python-docx")


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    values: list[str] = []
    for item in root.iter(f"{namespace}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{namespace}t")))
    return values


def _xlsx_sheet_names(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/workbook.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(raw)
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    return [sheet.get("name") or "" for sheet in root.iter(f"{namespace}sheet")]


def _xlsx_cell_text(cell: ElementTree.Element, shared: list[str], namespace: str) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{namespace}t")).strip()
    value_node = cell.find(f"{namespace}v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text.strip()
    if cell_type == "s":
        try:
            return shared[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    return raw


def _parse_xlsx(content: bytes) -> ParsedDocument:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ApiError(
            "DOCUMENT_PARSE_FAILED",
            "XLSX 解析失败，文件可能损坏或不是真正的 Excel 文档",
            422,
            {"parser": "openpyxl-free", "error": str(exc)[:240]},
        ) from exc

    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with archive:
        shared = _xlsx_shared_strings(archive)
        names = _xlsx_sheet_names(archive)
        sheet_paths = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )
        parts: list[str] = []
        for index, path in enumerate(sheet_paths):
            root = ElementTree.fromstring(archive.read(path))
            rows: list[str] = []
            for row in root.iter(f"{namespace}row"):
                cells = [
                    value
                    for cell in row.iter(f"{namespace}c")
                    if (value := _xlsx_cell_text(cell, shared, namespace))
                ]
                if cells:
                    rows.append(" | ".join(cells))
                if len(rows) >= MAX_SHEET_ROWS:
                    break
            if not rows:
                continue
            sheet_name = names[index] if index < len(names) else f"Sheet{index + 1}"
            parts.append(f"## {sheet_name}\n" + "\n".join(rows))

    warning = None
    if len(parts) == 0:
        warning = "工作簿中没有可提取的单元格文本"
    return ParsedDocument(
        text="\n\n".join(parts),
        parser="xlsx-zip",
        status=PARTIAL if warning else PARSED,
        warning=warning,
    )


def _parse_delimited(content: bytes, delimiter: str) -> ParsedDocument:
    text, encoding = _decode_text(content)
    rows: list[str] = []
    for index, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if index >= MAX_CSV_ROWS:
            break
        cells = [cell.strip() for cell in row if cell.strip()]
        if cells:
            rows.append(" | ".join(cells))
    warning = "表格为空" if not rows else None
    return ParsedDocument(
        text="\n".join(rows),
        parser=f"csv[{encoding}]",
        status=PARTIAL if warning else PARSED,
        warning=warning,
    )


def _parse_html(content: bytes) -> ParsedDocument:
    text, _encoding = _decode_text(content)
    extractor = _HTMLTextExtractor()
    extractor.feed(text)
    extractor.close()
    extracted = extractor.text()
    warning = "HTML 未提取到可见文本（可能全为脚本或样式）" if not extracted.strip() else None
    return ParsedDocument(
        text=extracted,
        parser="html.parser",
        status=PARTIAL if warning else PARSED,
        warning=warning,
    )


def _parse_plain(content: bytes) -> ParsedDocument:
    text, encoding = _decode_text(content)
    return ParsedDocument(text=text, parser=f"text[{encoding}]")


def parse_document(filename: str, content: bytes) -> ParsedDocument:
    """按扩展名分派解析器；未知格式显式拒绝，不静默按文本处理。"""

    if not content:
        raise ApiError("INVALID_ARGUMENT", "上传文件为空", 400)

    extension = extension_of(filename)
    if extension in REJECTED_EXTENSIONS:
        raise ApiError(
            "UNSUPPORTED_FORMAT",
            REJECTED_EXTENSIONS[extension],
            415,
            {"extension": extension, "supported": sorted(SUPPORTED_EXTENSIONS)},
        )

    if extension in PDF_EXTENSIONS:
        parsed = _parse_pdf(content)
    elif extension in DOCX_EXTENSIONS:
        parsed = _parse_docx(content)
    elif extension in XLSX_EXTENSIONS:
        parsed = _parse_xlsx(content)
    elif extension in CSV_EXTENSIONS:
        parsed = _parse_delimited(content, "\t" if extension == ".tsv" else ",")
    elif extension in HTML_EXTENSIONS:
        parsed = _parse_html(content)
    elif extension in TEXT_EXTENSIONS or not extension:
        parsed = _parse_plain(content)
    else:
        raise ApiError(
            "UNSUPPORTED_FORMAT",
            f"不支持的文件类型 {extension}，请转换为 PDF/DOCX/TXT/MD/CSV/XLSX 后重试",
            415,
            {"extension": extension, "supported": sorted(SUPPORTED_EXTENSIONS)},
        )

    parsed.text, parsed.truncated = _apply_budget(parsed.text)
    if parsed.truncated:
        truncated_note = f"正文超过 {MAX_TEXT_CHARS} 字符已截断"
        parsed.warning = (
            f"{parsed.warning}；{truncated_note}" if parsed.warning else truncated_note
        )
        parsed.status = PARTIAL
    if not parsed.text.strip():
        parsed.status = FAILED
        if not parsed.warning:
            parsed.warning = "解析结果为空，请确认文件内容或转交 OCR 处理"
    return parsed
