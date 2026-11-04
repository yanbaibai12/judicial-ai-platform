from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..models import McpServer, McpTool
from ..schemas import McpServerCreate, McpToolCreate, McpToolGrantCreate
from ..security import Principal, require_admin, require_any
from ..services.mcp_hub import (
    McpError,
    call_tool,
    create_server,
    create_tool,
    grant_tool,
    list_tools,
    tool_to_mcp,
)
from .utils import ok

router = APIRouter(tags=["mcp"])


def _server_dict(server: McpServer) -> dict[str, Any]:
    return {
        "id": server.id,
        "tenant_id": server.tenant_id,
        "code": server.code,
        "name": server.name,
        "transport": server.transport,
        "endpoint": server.endpoint,
        "auth_type": server.auth_type,
        "status": server.status,
        "owner": server.owner,
        "created_at": server.created_at.isoformat() if server.created_at else None,
    }


def _tool_dict(tool: McpTool) -> dict[str, Any]:
    return {
        "id": tool.id,
        "server_id": tool.server_id,
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "risk_level": tool.risk_level,
        "idempotent": tool.idempotent,
        "timeout_ms": tool.timeout_ms,
        "status": tool.status,
        "created_at": tool.created_at.isoformat() if tool.created_at else None,
    }


@router.post("/api/v1/admin/mcp-servers")
def register_server(
    payload: McpServerCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = create_server(db, payload.tenant_id or 0, payload)
    return ok(request, _server_dict(server), 201)


@router.get("/api/v1/admin/mcp-servers")
def list_servers(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    servers = db.query(McpServer).order_by(McpServer.created_at).all()
    return ok(request, {"items": [_server_dict(item) for item in servers], "total": len(servers)})


@router.post("/api/v1/admin/mcp-servers/{server_id}/tools")
def register_tool(
    server_id: int,
    payload: McpToolCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = db.get(McpServer, server_id)
    if server is None:
        raise ApiError("RESOURCE_NOT_FOUND", "MCP Server 不存在", 404)
    tool = create_tool(db, server, payload)
    return ok(request, _tool_dict(tool), 201)


@router.get("/api/v1/admin/mcp-tools")
def list_admin_tools(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tools = db.query(McpTool).order_by(McpTool.name).all()
    return ok(request, {"items": [_tool_dict(item) for item in tools], "total": len(tools)})


@router.post("/api/v1/admin/mcp-tools/{tool_id}/grants")
def create_tool_grant(
    tool_id: int,
    payload: McpToolGrantCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tool = db.get(McpTool, tool_id)
    if tool is None:
        raise ApiError("RESOURCE_NOT_FOUND", "工具不存在", 404)
    grant = grant_tool(db, tool, payload.app_id)
    return ok(
        request,
        {"id": grant.id, "app_id": grant.app_id, "tool_id": grant.tool_id, "status": grant.status},
        201,
    )


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _handle_rpc(
    payload: dict[str, Any],
    request: Request,
    principal: Principal,
    db: Session,
) -> dict[str, Any] | None:
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return _rpc_error(payload.get("id"), -32600, "Invalid Request")
    rpc_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params") or {}

    if method == "initialize":
        return _rpc_result(
            rpc_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "judicial-ai-mcp-hub", "version": "0.1.0"},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _rpc_result(rpc_id, {})
    if method == "tools/list":
        tools = list_tools(db, principal)
        return _rpc_result(rpc_id, {"tools": [tool_to_mcp(tool) for tool in tools]})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _rpc_error(rpc_id, -32602, "name/arguments 不合法")
        try:
            result = call_tool(
                db,
                principal,
                name,
                arguments,
                getattr(request.state, "request_id", ""),
            )
            return _rpc_result(rpc_id, result)
        except McpError as exc:
            return _rpc_error(rpc_id, exc.code, exc.message, exc.data)

    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


@router.post("/mcp")
def mcp_endpoint(
    payload: dict[str, Any] | list[dict[str, Any]],
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    if isinstance(payload, list):
        if not payload:
            return JSONResponse(_rpc_error(None, -32600, "Empty batch"), status_code=200)
        results = []
        for item in payload:
            result = _handle_rpc(item, request, principal, db)
            if result is not None:
                results.append(result)
        return JSONResponse(results, status_code=200)

    result = _handle_rpc(payload, request, principal, db)
    if result is None:
        return Response(status_code=202)
    return JSONResponse(result, status_code=200)
