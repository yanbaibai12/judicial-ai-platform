"""导出 OpenAPI 契约到 packages/contracts/openapi.json。

用法：uv run python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from judicial_ai.app import create_app  # noqa: E402


def main() -> int:
    app = create_app()
    spec = app.openapi()
    out = ROOT / "packages" / "contracts" / "openapi.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths = len(spec.get("paths", {}))
    print(f"exported {paths} paths -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
