from __future__ import annotations

import io
import uuid

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def _pdf_with_pages(pages: list[str]) -> bytes:
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_citations_keep_page_numbers_and_partial_parse_warning(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"pdf-{uuid.uuid4().hex[:8]}", "name": "PDF test"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    kb_id = response.json()["data"]["id"]
    content = _pdf_with_pages(["First page interest clause", "Second page payment clause", ""])
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={"file": ("sample.pdf", content, "application/pdf")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    upload = response.json()["data"]
    assert upload["page_count"] == 3
    assert upload["parse_status"] == "PARTIAL"
    assert "未提取" in upload["parse_warning"]
    detail = client.get(f"/api/v1/documents/{upload['document']['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["document_version"]["page_count"] == 3
    assert detail.json()["data"]["document_version"]["parse_status"] == "PARTIAL"
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "page clause", "top_k": 10},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert {item["citation"]["page"] for item in results} == {1, 2}
    assert all(item["metadata"]["pages_without_text"] == [3] for item in results)
    for item in results:
        source = client.get(item["citation"]["source_url"], headers=headers)
        assert source.status_code == 200, source.text
        assert source.json()["data"]["document_version"]["parse_status"] == "PARTIAL"
        chunks = source.json()["data"]["chunks"]
        cited = next(chunk for chunk in chunks if chunk["chunk_id"] == item["chunk_id"])
        assert cited["page_from"] == item["citation"]["page"]


def test_pdf_same_text_with_different_page_boundaries_is_not_deduplicated(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"pdf-dedup-{uuid.uuid4().hex[:8]}", "name": "PDF test"},
        headers=headers,
    )
    kb_id = response.json()["data"]["id"]
    for content in (
        _pdf_with_pages(["First text", "Second text"]),
        _pdf_with_pages(["First text", "", "Second text"]),
    ):
        response = client.post(
            f"/api/v1/kbs/{kb_id}/documents/upload",
            files={"file": ("sample.pdf", content, "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["duplicate"] is False
