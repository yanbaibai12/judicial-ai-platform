from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
client_ip_var: ContextVar[str] = ContextVar("client_ip", default="")
user_agent_var: ContextVar[str] = ContextVar("user_agent", default="")


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    trace_id: str
    client_ip: str
    user_agent: str


def current_request_context() -> RequestContext:
    return RequestContext(
        request_id=request_id_var.get(),
        trace_id=trace_id_var.get(),
        client_ip=client_ip_var.get(),
        user_agent=user_agent_var.get(),
    )


def bind_request_context(
    *, request_id: str, trace_id: str, client_ip: str, user_agent: str
) -> tuple[Token[str], Token[str], Token[str], Token[str]]:
    return (
        request_id_var.set(request_id),
        trace_id_var.set(trace_id),
        client_ip_var.set(client_ip),
        user_agent_var.set(user_agent),
    )


def reset_request_context(tokens: tuple[Token[str], Token[str], Token[str], Token[str]]) -> None:
    request_id_token, trace_id_token, client_ip_token, user_agent_token = tokens
    request_id_var.reset(request_id_token)
    trace_id_var.reset(trace_id_token)
    client_ip_var.reset(client_ip_token)
    user_agent_var.reset(user_agent_token)
