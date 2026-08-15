from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backup_postgres import (
    default_backup_dir,
    latest_manifest,
    load_env_files,
    restore_backup_package,
)


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Restore a verified backup package into an independent PostgreSQL database.")
    parser.add_argument("--manifest", default=None, help="Path to backup_manifest.json. Defaults to latest.")
    parser.add_argument("--backup-dir", default=str(default_backup_dir()))
    parser.add_argument("--source-database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--restore-database-url", default=os.environ.get("RESTORE_DATABASE_URL"))
    parser.add_argument("--restore-upload-dir", default=os.environ.get("RESTORE_TEST_UPLOAD_DIR"))
    parser.add_argument("--keep-owner", action="store_true", help="Do not pass --no-owner to pg_restore.")
    args = parser.parse_args()
    if not args.source_database_url:
        raise SystemExit("DATABASE_URL is required")
    if not args.restore_database_url:
        raise SystemExit("RESTORE_DATABASE_URL is required")
    if not args.restore_upload_dir:
        raise SystemExit("RESTORE_TEST_UPLOAD_DIR is required")
    manifest_path = Path(args.manifest) if args.manifest else latest_manifest(Path(args.backup_dir))
    result = restore_backup_package(
        manifest_path=manifest_path,
        source_database_url=args.source_database_url,
        restore_database_url=args.restore_database_url,
        restore_upload_dir=Path(args.restore_upload_dir),
        no_owner=not args.keep_owner,
    )
    print(json.dumps({"ok": result["ok"], "manifest_path": str(manifest_path), "restore_result": str(manifest_path.parent / "restore_result.json")}, ensure_ascii=False))
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
