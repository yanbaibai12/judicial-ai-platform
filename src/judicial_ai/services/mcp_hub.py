from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..ids import new_id
from ..models import McpServer, McpTool, McpToolGrant, ToolCallLog
from ..schemas import McpServerCreate, McpToolCreate
from ..security import Principal
from .capabilities import validate_against_schema, validate_json_schema
from .observability import record_event


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def create_server(db: Session, tenant_id: int, payload: McpServerCreate) -> McpServer:
    existing = (
        db.query(McpServer)
        .filter(McpServer.tenant_id == tenant_id, McpServer.code == payload.code)
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "MCP Server 编码已存在", 409)
    server = McpServer(
        id=new_id(),
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        transport=payload.transport,
        endpoint=payload.endpoint,
        auth_type=payload.auth_type,
        secret_ref=payload.secret_ref,
        owner=payload.owner,
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def create_tool(db: Session, server: McpServer, payload: McpToolCreate) -> McpTool:
    validate_json_schema(payload.input_schema, "input_schema")
    if payload.output_schema:
        validate_json_schema(payload.output_schema, "output_schema")
    existing = (
        db.query(McpTool)
        .filter(McpTool.server_id == server.id, McpTool.name == payload.name)
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "工具名称已存在", 409)
    tool = McpTool(
        id=new_id(),
        server_id=server.id,
        name=payload.name,
        description=payload.description,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        risk_level=payload.risk_level,
        idempotent=payload.idempotent,
        timeout_ms=payload.timeout_ms,
    )
    db.add(tool)
    db.commit()
    db.refresh(tool)
    return tool


def grant_tool(db: Session, tool: McpTool, app_id: int) -> McpToolGrant:
    existing = (
        db.query(McpToolGrant)
        .filter(McpToolGrant.app_id == app_id, McpToolGrant.tool_id == tool.id)
        .first()
    )
    if existing:
        return existing
    grant = McpToolGrant(id=new_id(), app_id=app_id, tool_id=tool.id)
    db.add(grant)
    db.commit()
    db.refresh(grant)
    return grant


def list_tools(db: Session, principal: Principal) -> list[McpTool]:
    query = (
        db.query(McpTool)
        .join(McpServer, McpTool.server_id == McpServer.id)
        .filter(McpTool.status == "ACTIVE", McpServer.status == "ACTIVE")
    )
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.join(McpToolGrant, McpToolGrant.tool_id == McpTool.id).filter(
            McpToolGrant.app_id == principal.app_id,
            McpToolGrant.status == "ACTIVE",
        )
    return query.order_by(McpTool.name).all()


def _tool_name(tool: McpTool, server: McpServer) -> str:
    return tool.name


def tool_to_mcp(tool: McpTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.input_schema or {"type": "object"},
    }


def _auth_headers(server: McpServer) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if server.auth_type and server.auth_type.upper() == "BEARER" and server.secret_ref:
        token = os.getenv(server.secret_ref)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def call_tool(
    db: Session,
    principal: Principal,
    tool_name: str,
    arguments: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    tool = (
        db.query(McpTool)
        .join(McpServer, McpTool.server_id == McpServer.id)
        .filter(McpTool.name == tool_name, McpTool.status == "ACTIVE", McpServer.status == "ACTIVE")
        .first()
    )
    if tool is None:
        raise McpError(-32601, f"工具不存在: {tool_name}")

    server = db.get(McpServer, tool.server_id)
    if server is None:
        raise McpError(-32603, "MCP Server 不存在")

    if (principal.kind in {"app", "user"} and not principal.is_admin):
        grant = (
            db.query(McpToolGrant)
            .filter(
                McpToolGrant.app_id == principal.app_id,
                McpToolGrant.tool_id == tool.id,
                McpToolGrant.status == "ACTIVE",
            )
            .first()
        )
        if grant is None:
            raise McpError(-32003, f"应用无权调用工具: {tool_name}")

    try:
        validate_against_schema(tool.input_schema or {}, arguments, "arguments")
    except ApiError as exc:
        raise McpError(-32602, exc.message, exc.details) from exc

    started = time.perf_counter()
    status = "SUCCESS"
    error_code = None
    result: dict[str, Any] | None = None
    try:
        if server.transport == "MOCK":
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "tool": tool.name,
                                "arguments": arguments,
                                "message": "Mock 工具调用成功",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": False,
            }
        elif server.transport in {"HTTP", "SSE"}:
            if not server.endpoint:
                raise McpError(-32603, "MCP Server 未配置 endpoint")
            response = httpx.post(
                server.endpoint,
                json=arguments,
                headers=_auth_headers(server),
                timeout=tool.timeout_ms / 1000,
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                data = response.json()
                text = json.dumps(data, ensure_ascii=False)
            else:
                text = response.text
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            raise McpError(-32603, f"暂不支持的传输方式: {server.transport}")
    except McpError:
        status = "FAILED"
        error_code = "TOOL_EXECUTION_FAILED"
        raise
    except httpx.TimeoutException as exc:
        status = "FAILED"
        error_code = "UPSTREAM_TIMEOUT"
        raise McpError(-32000, "工具调用超时") from exc
    except httpx.HTTPStatusError as exc:
        status = "FAILED"
        error_code = "TOOL_EXECUTION_FAILED"
        raise McpError(
            -32000,
            "工具调用失败",
            {"upstream_status": exc.response.status_code},
        ) from exc
    except Exception as exc:
        status = "FAILED"
        error_code = "TOOL_EXECUTION_FAILED"
        raise McpError(-32603, "工具执行异常") from exc
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        log = ToolCallLog(
            id=new_id(),
            request_id=request_id,
            tenant_id=principal.tenant_id or 0,
            app_id=principal.app_id or 0,
            tool_id=tool.id,
            params_hash=hashlib.sha256(
                json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            status=status,
            error_code=error_code,
            latency_ms=latency_ms,
        )
        db.add(log)
        db.commit()
        try:
            record_event(
                db,
                "TOOL_CALLED",
                tenant_id=principal.tenant_id or 0,
                app_id=principal.app_id,
                request_id=request_id,
                trace_id=request_id,
                payload={
                    "tool": tool.name,
                    "server": server.code,
                    "arguments": arguments,
                    "status": status,
                    "error_code": error_code,
                    "latency_ms": latency_ms,
                    "result_preview": (
                        json.dumps(result, ensure_ascii=False) if result else None
                    ),
                    "security_level": "INTERNAL",
                },
                object_ref=f"mcp:{server.code}:{tool.name}",
            )
        except Exception:
            pass

    if result is None:
        raise McpError(-32603, "工具执行未返回结果")
    return result
