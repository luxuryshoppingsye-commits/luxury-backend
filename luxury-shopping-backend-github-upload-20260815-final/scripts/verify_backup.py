from __future__ import annotations

import argparse
import json
from pathlib import Path

from backup_postgres import default_backup_dir, latest_manifest, load_env_files, verify_backup_manifest


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Verify a PostgreSQL backup manifest and upload archive.")
    parser.add_argument("--manifest", default=None, help="Path to backup_manifest.json. Defaults to latest.")
    parser.add_argument("--backup-dir", default=str(default_backup_dir()))
    args = parser.parse_args()
    manifest_path = Path(args.manifest) if args.manifest else latest_manifest(Path(args.backup_dir))
    result = verify_backup_manifest(manifest_path)
    output_path = manifest_path.parent / "verify_result.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "manifest_path": str(manifest_path), "result_path": str(output_path)}, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
