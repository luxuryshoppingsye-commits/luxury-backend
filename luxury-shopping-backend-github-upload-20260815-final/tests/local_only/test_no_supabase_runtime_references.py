from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_PATHS = [
    ROOT / "backend" / "app",
    ROOT / "lib",
    ROOT / "web",
    ROOT / "android",
    ROOT / "ios",
    ROOT / "windows",
]
FORBIDDEN = (
    "supabase.co",
    "/rest/v1",
    "/auth/v1",
    "/storage/v1",
    "/realtime/v1",
    "/functions/v1",
    "supabase_flutter",
    "@supabase",
    "SupabaseClient",
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_DB_URL",
)


def test_runtime_code_has_no_supabase_dependency_or_endpoint() -> None:
    findings: list[str] = []
    for base in RUNTIME_PATHS:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".dart", ".py", ".html", ".js", ".kt", ".java", ".swift", ".xml", ".gradle"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            lowered = text.lower()
            for token in FORBIDDEN:
                if token.lower() in lowered:
                    findings.append(f"{path.relative_to(ROOT)} contains {token}")
    assert findings == []
