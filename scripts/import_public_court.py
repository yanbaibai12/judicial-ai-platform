"""Import selected public Supreme People's Court pages into an isolated instance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

SOURCES = (
    "https://www.court.gov.cn/fabu/xiangqing/512441.html",
    "https://www.court.gov.cn/fabu/xiangqing/508001.html",
    "https://www.court.gov.cn/fabu/xiangqing/508041.html",
    "https://www.court.gov.cn/fabu/xiangqing/508011.html",
)


class CourtPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section = ""
        self.depth = 0
        self.title: list[str] = []
        self.body: list[str] = []
        self.date: list[str] = []
        self.in_li = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class", "") or ""
        if tag == "div":
            if self.section:
                self.depth += 1
            elif "txt_txt" in classes.split():
                self.section, self.depth = "body", 1
            elif "title" in classes.split() and not self.title:
                self.section, self.depth = "title", 1
        if tag == "li":
            self.in_li = True
        if self.section == "body" and tag in {"br", "p"}:
            self.body.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self.in_li = False
        if tag == "div" and self.section:
            self.depth -= 1
            if self.depth == 0:
                self.section = ""
        if self.section == "body" and tag == "p":
            self.body.append("\n")

    def handle_data(self, data: str) -> None:
        if self.section == "title":
            self.title.append(data)
        elif self.section == "body":
            self.body.append(data)
        if self.in_li and "发布时间：" in data:
            self.date.append(data.strip().removeprefix("发布时间："))

    def document(self) -> tuple[str, str, str]:
        title = "".join(self.title).strip()
        text = re.sub(r"\n[ \t\u3000]*\n+", "\n\n", "".join(self.body)).strip()
        if not title or len(text) < 200 or not self.date:
            raise ValueError("court page is missing title, publication date, or article content")
        return title, text, self.date[0]


def fetch_documents() -> list[dict]:
    documents = []
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for url in SOURCES:
            if urlparse(url).hostname != "www.court.gov.cn":
                raise ValueError("unexpected source host")
            response = client.get(url)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"unexpected source status: {response.status_code}")
            page = CourtPage()
            page.feed(response.content.decode("utf-8"))
            title, text, published_at = page.document()
            documents.append({
                "title": title,
                "text": text,
                "doc_type": "public_judicial_material",
                "source": "supreme_peoples_court",
                "source_ref": url,
                "security_level": "PUBLIC",
                "metadata": {
                    "publisher": "最高人民法院",
                    "published_at": published_at,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "source_sha256": hashlib.sha256(response.content).hexdigest(),
                    "legal_effect_status": "UNVERIFIED",
                },
            })
    return documents


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8098")
    args = parser.parse_args()
    admin_token = os.environ.get("JAI_IMPORT_ADMIN_TOKEN")
    if not admin_token:
        raise SystemExit("Set JAI_IMPORT_ADMIN_TOKEN before importing.")
    documents = fetch_documents()
    suffix = uuid.uuid4().hex[:8]
    admin = {"Authorization": f"Bearer {admin_token}"}
    with httpx.Client(base_url=args.base_url, timeout=90) as client:
        def post(path: str, body: dict, headers: dict, expected: int = 201) -> dict:
            response = client.post(path, json=body, headers=headers)
            if response.status_code != expected:
                raise RuntimeError(f"{path}: HTTP {response.status_code}: {response.text[:300]}")
            return response.json()["data"]

        tenant = post("/api/v1/admin/tenants", {
            "code": f"public-court-{suffix}", "name": "公开司法资料",
            "type": "PROJECT", "metadata": {"purpose": "local public source import"},
        }, admin)
        app = post("/api/v1/admin/apps", {
            "tenant_id": tenant["id"], "code": f"court-import-{suffix}",
            "name": "公开司法资料检索", "permissions": [
                "app:invoke", "kb:read", "kb:write", "kb:promote", "job:read",
            ],
        }, admin)
        credential = post(f"/api/v1/admin/apps/{app['id']}/credentials", {}, admin)
        app_token = credential["api_key"]
        app_headers = {"Authorization": f"Bearer {app_token}"}
        kb = post("/api/v1/kbs", {
            "code": f"court-public-{suffix}", "name": "最高人民法院公开司法资料",
            "description": "官网原文，采集时保存来源；法律时效与适用性须人工核验。",
            "security_level": "PUBLIC",
        }, app_headers)
        for document in documents:
            post(f"/api/v1/kbs/{kb['id']}/documents", document, app_headers)
        version = f"public-{datetime.now(UTC):%Y%m%d}-{suffix}"
        post(f"/api/v1/kbs/{kb['id']}/versions", {"version": version, "publish": True}, app_headers)
        job = post(
            f"/api/v1/kbs/{kb['id']}/index-jobs?version={version}", {}, app_headers, expected=202,
        )
        for _ in range(90):
            response = client.get(f"/api/v1/jobs/{job['id']}", headers=app_headers)
            response.raise_for_status()
            job = response.json()["data"]
            if job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD"}:
                break
            time.sleep(1)
        if job["status"] != "SUCCEEDED":
            raise RuntimeError(f"index job did not succeed: {job['status']}")
        index = job["result"]["index"]
        if index["status"] != "READY":
            raise RuntimeError(f"index requires independent approval: {index['status']}")
        post(f"/api/v1/kbs/{kb['id']}/aliases", {
            "alias": "prod", "kb_version": version, "index_version": index["version"],
        }, app_headers, expected=200)
        capability_code = f"rag.public-court-{suffix}"
        post("/api/v1/admin/capabilities", {
            "code": capability_code, "name": "公开司法资料检索（本地）",
            "type": "RAG_RETRIEVE", "owner": "local-console",
            "description": "SQL 检索及模拟向量，本地演示；原文来自最高人民法院官网。",
            "version": "1.0.0", "protocol": "PLATFORM",
            "request_schema": {"type": "object"}, "response_schema": {"type": "object"},
            "tags": ["public", "court", "local"],
        }, admin)
        post(f"/api/v1/admin/capabilities/{capability_code}/versions/1.0.0/publish",
             {}, admin, expected=200)
        post(f"/api/v1/admin/capabilities/{capability_code}/grants",
             {"app_id": app["id"], "data_scope": {"kb_ids": [kb["id"]]}}, admin)
        retrieval = client.post("/v1/retrieve", headers=app_headers, json={
            "kb_ids": [kb["id"]], "query": "生态环境法典时间效力", "top_k": 3,
        })
        retrieval.raise_for_status()
        payload = retrieval.json()
        if not payload.get("results"):
            raise RuntimeError("import succeeded but verification retrieval returned no results")
        print(json.dumps({
            "tenant_id": tenant["id"], "kb_id": kb["id"], "document_count": len(documents),
            "titles": [item["title"] for item in documents],
            "capability_code": capability_code, "app_token": app_token,
            "retrieval_results": len(payload["results"]),
            "citation_status": payload.get("citation", {}).get("status"),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
