from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app.api.routes.e2e_verification import (
    _project_row,
    require_e2e_verification_enabled,
)


class _Settings:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def require_test_fixtures_enabled(self, operation: str) -> None:
        if not self.allowed:
            raise RuntimeError(f"{operation} blocked")


def test_e2e_verification_guard_rejects_non_test_environment() -> None:
    with pytest.raises(HTTPException) as exc:
        require_e2e_verification_enabled(_Settings(False))  # type: ignore[arg-type]

    assert exc.value.status_code == 403
    assert "E2E verification blocked" in str(exc.value.detail)


def test_e2e_verification_guard_allows_isolated_test_environment() -> None:
    require_e2e_verification_enabled(_Settings(True))  # type: ignore[arg-type]


def test_project_row_returns_requested_fields_only() -> None:
    row = {"id": "row-1", "name": "Visible name", "secret": "hidden"}

    assert _project_row(row, ["id", "name"]) == {
        "id": "row-1",
        "name": "Visible name",
    }


def test_project_row_without_fields_returns_full_row() -> None:
    row = {"id": "row-1", "name": "Visible name"}

    assert _project_row(row, []) == row
