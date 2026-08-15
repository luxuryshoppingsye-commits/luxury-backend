from __future__ import annotations

import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from backup_postgres import assert_safe_restore_target, replace_database


def test_restore_target_cannot_equal_source_database():
    source = "postgresql://luxury_admin@127.0.0.1:55432/luxury_test"
    with pytest.raises(RuntimeError, match="source database"):
        assert_safe_restore_target(source, source)


def test_restore_target_must_use_restore_test_prefix():
    source = "postgresql://luxury_admin@127.0.0.1:55432/luxury_test"
    target = replace_database(source, "luxury_test_copy")
    with pytest.raises(RuntimeError, match="must start"):
        assert_safe_restore_target(source, target)


def test_restore_target_accepts_independent_restore_database_name():
    source = "postgresql://luxury_admin@127.0.0.1:55432/luxury_test"
    target = replace_database(source, "luxury_shopping_restore_test_pytest")
    assert_safe_restore_target(source, target)
