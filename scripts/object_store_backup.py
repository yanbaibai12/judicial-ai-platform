from __future__ import annotations

import argparse
import json

from judicial_ai.services.object_store import export_backup, get_object_store, restore_backup


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup or restore original document objects")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("path")
    restore = subparsers.add_parser("restore")
    restore.add_argument("path")
    restore.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    store = get_object_store()
    if args.command == "backup":
        result = export_backup(store, args.path)
    else:
        result = restore_backup(store, args.path, overwrite=args.overwrite)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
