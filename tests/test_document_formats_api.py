from __future__ import annotations

import io
import uuid
import zipfile

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _create_kb(client, headers, prefix: str) -> int:
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"{prefix}-{uuid.uuid4().hex[:8]}", "name": f"{prefix} test"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _xlsx_bytes() -> bytes:
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_NS}"><sheets>'
        '<sheet name="规则" sheetId="1"/></sheets></workbook>'
    )
    sheet = (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>利率上限</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>四倍</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def test_upload_markdown_and_html_are_parsed(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers, "md")

    cases = [
        ("clause.md", "# 借款合同\n\n利率不得超过四倍。".encode()),
        (
            "clause.html",
            "<html><body><p>借款利率不得超过四倍</p></body></html>".encode(),
        ),
    ]
    for filename, content in cases:
        response = client.post(
            f"/api/v1/kbs/{kb_id}/documents/upload",
            files={"file": (filename, content, "text/plain")},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["parse_status"] == "PARSED"
        assert data["parse_warning"] is None
        detail = client.get(f"/api/v1/documents/{data['document']['id']}", headers=headers)
        assert detail.status_code == 200, detail.text
        parser = detail.json()["data"]["document"]["metadata"]["parse_parser"]
        assert parser.startswith("text") or parser == "html.parser"


def test_upload_csv_and_xlsx_are_parsed(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers, "sheet")

    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={"file": ("rules.csv", "条款,阈值\n利率,四倍\n".encode(), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["parse_status"] == "PARSED"

    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={
            "file": (
                "rules.xlsx",
                _xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["parse_status"] == "PARSED"
    assert data["chunk_count"] >= 1


def test_upload_unsupported_format_returns_415_and_stores_nothing(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers, "legacy")

    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={"file": ("contract.doc", b"\xd0\xcf\x11\xe0binary", "application/msword")},
        headers=headers,
    )
    assert response.status_code == 415, response.text
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_FORMAT"
    assert ".docx" in body["error"]["message"]

    listing = client.get(f"/api/v1/kbs/{kb_id}/documents", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["total"] == 0


def test_upload_image_requires_ocr(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers, "scan")

    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        headers=headers,
    )
    assert response.status_code == 415, response.text
    assert "OCR" in response.json()["error"]["message"]


def test_retrieve_exposes_citation_status(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers, "cite")

    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={
            "title": "借款合同利率条款",
            "text": "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。"
            "双方约定的利率超过合同成立时一年期贷款市场报价利率四倍的除外。",
            "doc_type": "law",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "借款利率四倍", "top_k": 5},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["citation"]["status"] in {"VERIFIED", "NONE"}
    assert payload["citation"]["review_required"] is False
    assert payload["citation"]["counts"]["VERIFIED"] == len(payload["results"])
    for item in payload["results"]:
        assert item["citation_status"] == "VERIFIED"
        assert item["citation"]["status"] == "VERIFIED"
