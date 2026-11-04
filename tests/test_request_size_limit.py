from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import StreamingResponse

from judicial_ai.config import get_settings
from judicial_ai.middleware import RequestContextMiddleware


@pytest.fixture
def size_limited_app(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_request_bytes", 512)
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.state.calls = 0

    @app.post("/json")
    async def json_body(request: Request):
        body = await request.json()
        app.state.calls += 1
        return body

    @app.post("/upload")
    async def upload(file: UploadFile = File()):
        body = await file.read()
        app.state.calls += 1
        return {"size": len(body)}

    @app.post("/stream")
    async def stream(request: Request):
        body = await request.body()

        async def generate():
            yield body
            yield b"-end"

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def test_chunked_body_limit_and_request_context(size_limited_app):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=size_limited_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/json",
                content=chunks(b'{"value":"', b"x" * 510, b'"}'),
                headers={"content-type": "application/json", "x-request-id": "limit-test"},
            )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
        assert response.json()["error"]["request_id"] == "limit-test"
        assert response.headers["x-request-id"] == "limit-test"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert size_limited_app.state.calls == 0

    anyio.run(run)


@pytest.mark.parametrize("declared_length", ["1", "invalid", "-1"])
def test_untrustworthy_content_length_does_not_bypass_limit(size_limited_app, declared_length):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=size_limited_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/json",
                content=chunks(b"x" * 300, b"x" * 300),
                headers={"content-length": declared_length},
            )
        assert response.status_code == 413
        assert size_limited_app.state.calls == 0

    anyio.run(run)


def test_multipart_limit_and_valid_upload(size_limited_app):
    async def run():
        boundary = b"test-boundary"
        prefix = (
            b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; '
            b'filename="test.txt"\r\nContent-Type: text/plain\r\n\r\n'
        )
        suffix = b"\r\n--" + boundary + b"--\r\n"
        headers = {"content-type": "multipart/form-data; boundary=test-boundary"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=size_limited_app), base_url="http://test"
        ) as client:
            rejected = await client.post(
                "/upload", headers=headers, content=chunks(prefix, b"x" * 512, suffix)
            )
            accepted = await client.post(
                "/upload", headers=headers, content=chunks(prefix, b"hello", suffix)
            )
        assert rejected.status_code == 413
        assert accepted.status_code == 200
        assert accepted.json() == {"size": 5}
        assert size_limited_app.state.calls == 1

    anyio.run(run)


def test_exact_limit_json_and_streamed_response(size_limited_app):
    async def run():
        body = b'{"value":"' + b"x" * 500 + b'"}'
        assert len(body) == 512
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=size_limited_app), base_url="http://test"
        ) as client:
            accepted = await client.post(
                "/json", content=chunks(body[:256], body[256:]),
                headers={"content-type": "application/json"},
            )
            streamed = await client.post("/stream", content=chunks(b"one", b"-two"))
            rejected = await client.post(
                "/json", content=b"{}", headers={"content-length": "513"}
            )
        assert accepted.status_code == 200
        assert accepted.json()["value"] == "x" * 500
        assert streamed.status_code == 200
        assert streamed.text == "one-two-end"
        assert rejected.status_code == 413
        assert size_limited_app.state.calls == 1

    anyio.run(run)
