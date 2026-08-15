from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backup_postgres import compare_snapshots, database_snapshot, load_env_files, relationship_checks


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Compare source and restored PostgreSQL databases by row counts, hashes, sums, and relationships.")
    parser.add_argument("--source-database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--restore-database-url", default=os.environ.get("RESTORE_DATABASE_URL"))
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    if not args.source_database_url:
        raise SystemExit("DATABASE_URL is required")
    if not args.restore_database_url:
        raise SystemExit("RESTORE_DATABASE_URL is required")
    source = database_snapshot(args.source_database_url)
    restored = database_snapshot(args.restore_database_url)
    comparison = compare_snapshots(source, restored)
    relationships = relationship_checks(args.restore_database_url)
    result = {"database_compare": comparison, "relationship_checks": relationships, "ok": comparison["ok"] and relationships["ok"]}
    output_path = Path(args.output) if args.output else Path("database_compare_result.json")
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"ok": result["ok"], "output": str(output_path)}, ensure_ascii=False))
    return 0 if result["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
