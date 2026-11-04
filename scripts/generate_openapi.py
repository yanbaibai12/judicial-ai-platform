from __future__ import annotations

import json
from pathlib import Path

from judicial_ai.app import create_app


def main() -> None:
    contract = create_app().openapi()
    target = Path("packages/contracts/openapi/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {target} ({len(contract.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()
