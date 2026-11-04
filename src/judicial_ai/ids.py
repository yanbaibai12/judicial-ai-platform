"""雪花风格 ID 生成器。

ID 结构：41 位毫秒时间戳 + 10 位节点号 + 12 位序列号。
多副本部署时必须为每个进程配置唯一的 NODE_ID（0-1023）；未配置时按主机名
与网卡地址派生，适用于单机多进程，但跨主机部署仍建议显式配置。
"""

from __future__ import annotations

import socket
import threading
import time
import uuid

from .config import get_settings

_EPOCH_MS = 1704067200000  # 2024-01-01T00:00:00Z
_last_ms = 0
_sequence = 0
_lock = threading.Lock()


def _node_id() -> int:
    configured = get_settings().node_id
    if 0 <= configured <= 1023:
        return configured
    seed = f"{socket.gethostname()}:{uuid.getnode()}"
    return int.from_bytes(seed.encode("utf-8"), "big") % 1024


_NODE_ID = _node_id()


def new_id() -> int:
    global _last_ms, _sequence
    with _lock:
        now = int(time.time() * 1000)
        if now == _last_ms:
            _sequence = (_sequence + 1) & 0xFFF
            if _sequence == 0:
                while now <= _last_ms:
                    now = int(time.time() * 1000)
        else:
            _sequence = 0
        _last_ms = max(now, _last_ms)
        return ((_last_ms - _EPOCH_MS) << 22) | (_NODE_ID << 12) | _sequence
