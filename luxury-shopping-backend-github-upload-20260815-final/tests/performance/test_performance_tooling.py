from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_performance_suite_files_exist() -> None:
    expected = [
        "performance/scripts/smoke.js",
        "performance/scripts/load.js",
        "performance/scripts/stress.js",
        "performance/scripts/spike.js",
        "performance/scripts/soak.js",
        "performance/helpers/auth.js",
        "performance/helpers/http.js",
        "backend/scripts/seed_performance_data.py",
        "performance/scripts/collect_metrics.py",
        "performance/scripts/db_integrity_check.py",
    ]
    missing = [path for path in expected if not (PROJECT_ROOT / path).exists()]
    assert missing == []


def test_performance_scripts_do_not_store_secrets() -> None:
    checked_roots = [PROJECT_ROOT / "performance", PROJECT_ROOT / "backend" / "scripts" / "seed_performance_data.py"]
    forbidden = ["Test512", "DATABASE_URL=", "JWT_SECRET="]
    findings: list[str] = []
    for root in checked_roots:
        files = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in files:
            if path.suffix.lower() not in {".js", ".json", ".md", ".ps1", ".sh", ".py"}:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    findings.append(f"{path.relative_to(PROJECT_ROOT)} contains {needle}")
    assert findings == []
