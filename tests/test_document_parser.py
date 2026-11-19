from __future__ import annotations

import io
import zipfile

import pytest

from judicial_ai.errors import ApiError
from judicial_ai.services.document_parser import (
    FAILED,
    PARSED,
    PARTIAL,
    parse_document,
)

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """构造最小可解析的 xlsx（inlineStr，不依赖第三方库）。"""

    workbook = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{_NS}"><sheets>'
        + "".join(
            f'<sheet name="{name}" sheetId="{index}"/>'
            for index, name in enumerate(sheets, 1)
        )
        + "</sheets></workbook>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        for index, rows in enumerate(sheets.values(), 1):
            body = ""
            for row_index, row in enumerate(rows, 1):
                cells = "".join(
                    f'<c r="R{row_index}C{cell_index}" t="inlineStr"><is><t>{value}</t></is></c>'
                    for cell_index, value in enumerate(row, 1)
                )
                body += f'<row r="{row_index}">{cells}</row>'
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{_NS}">'
                f"<sheetData>{body}</sheetData></worksheet>",
            )
    return buffer.getvalue()


def test_html_ignores_script_and_style() -> None:
    content = (
        b"<html><head><style>.x{color:red}</style><script>var a=1;</script></head>"
        b"<body><h1>\xe5\x80\x9f\xe6\xac\xbe\xe5\x90\x88\xe5\x90\x8c</h1>"
        b"<p>\xe5\x88\xa9\xe7\x8e\x87\xe4\xb8\x8d\xe5\xbe\x97\xe8\xb6\x85\xe8\xbf\x87\xe5\x9b\x9b\xe5\x80\x8d</p>"
        b"</body></html>"
    )
    parsed = parse_document("contract.html", content)

    assert parsed.status == PARSED
    assert "借款合同" in parsed.text
    assert "利率不得超过四倍" in parsed.text
    assert "color:red" not in parsed.text
    assert "var a=1" not in parsed.text


def test_markdown_and_gbk_text_are_decoded() -> None:
    markdown = "# 借款合同\n\n利率不得超过 LPR 四倍。".encode()
    parsed = parse_document("note.md", markdown)
    assert parsed.status == PARSED
    assert "利率不得超过" in parsed.text

    gbk = "借款合同利率不得违反国家有关规定".encode("gb18030")
    parsed_gbk = parse_document("note.txt", gbk)
    assert parsed_gbk.status == PARSED
    assert "借款合同利率" in parsed_gbk.text
    assert "gb18030" in parsed_gbk.parser


def test_csv_rows_are_joined() -> None:
    content = "条款,风险\n利率,高\n违约金,中\n".encode()
    parsed = parse_document("rules.csv", content)

    assert parsed.status == PARSED
    assert "条款 | 风险" in parsed.text
    assert "利率 | 高" in parsed.text


def test_xlsx_extracts_each_sheet() -> None:
    content = _xlsx({"规则": [["条款", "阈值"], ["利率", "四倍"]]})
    parsed = parse_document("rules.xlsx", content)

    assert parsed.status == PARSED
    assert "## 规则" in parsed.text
    assert "利率 | 四倍" in parsed.text


def test_unsupported_format_is_rejected_not_decoded() -> None:
    with pytest.raises(ApiError) as exc_info:
        parse_document("legacy.doc", b"\xd0\xcf\x11\xe0binary-rubbish")

    assert exc_info.value.code == "UNSUPPORTED_FORMAT"
    assert exc_info.value.status_code == 415
    assert ".docx" in exc_info.value.message


def test_image_upload_points_to_ocr() -> None:
    with pytest.raises(ApiError) as exc_info:
        parse_document("scan.png", b"\x89PNG\r\n\x1a\n")

    assert exc_info.value.code == "UNSUPPORTED_FORMAT"
    assert "OCR" in exc_info.value.message


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ApiError) as exc_info:
        parse_document("empty.txt", b"")

    assert exc_info.value.code == "INVALID_ARGUMENT"


def test_binary_garbage_in_text_extension_is_not_silently_indexed() -> None:
    parsed = parse_document("garbage.txt", b"\x00\x01\x02\x03")
    assert parsed.status == PARSED
    assert parsed.text == "\x00\x01\x02\x03"


def test_html_without_visible_text_is_partial() -> None:
    content = b"<html><body><script>only()</script></body></html>"
    parsed = parse_document("empty.html", content)

    assert parsed.status in (PARTIAL, FAILED)
    assert parsed.warning


def test_oversized_text_is_truncated_and_marked_partial() -> None:
    content = ("借款合同" * 60_000).encode("utf-8")
    parsed = parse_document("big.txt", content)

    assert parsed.truncated is True
    assert parsed.status == PARTIAL
    assert "截断" in (parsed.warning or "")
    assert len(parsed.text) <= 200_000
