from __future__ import annotations

import asyncio
import sys

import pytest
import pytest_asyncio
from sqlalchemy import text

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    from backend.app.config import get_settings
    from backend.app.database import SessionFactory, engine
except ModuleNotFoundError:
    import importlib
    import sys
    import types

    app_package = importlib.import_module("app")
    backend_package = types.ModuleType("backend")
    backend_package.app = app_package
    sys.modules.setdefault("backend", backend_package)
    sys.modules.setdefault("backend.app", app_package)
    # Keep backend.app.main identical to app.main in a standalone checkout.
    # Without this alias, pytest can load two module objects and monkeypatches
    # applied to backend.app.main do not affect imported function globals.
    app_main = importlib.import_module("app.main")
    sys.modules.setdefault("backend.app.main", app_main)

    from app.config import get_settings
    from app.database import SessionFactory, engine


_LIVE_BACKEND_TEST_PATHS = (
    "backend\\tests\\admin\\",
    "backend/tests/admin/",
    "backend\\tests\\api\\",
    "backend/tests/api/",
    "backend\\tests\\concurrency\\",
    "backend/tests/concurrency/",
    "backend\\tests\\financial\\",
    "backend/tests/financial/",
    "backend\\tests\\security\\",
    "backend/tests/security/",
    "backend\\tests\\test_api_flows.py",
    "backend/tests/test_api_flows.py",
    "backend\\tests\\test_full_system_postgres.py",
    "backend/tests/test_full_system_postgres.py",
)


def pytest_runtest_setup(item: pytest.Item) -> None:
    test_path = str(item.fspath)
    if not any(path in test_path for path in _LIVE_BACKEND_TEST_PATHS):
        return
    settings = get_settings()
    if settings.is_test_environment:
        return
    pytest.fail(
        "Refusing to run live FastAPI/PostgreSQL tests outside APP_ENV=test, "
        "ALLOW_TEST_FIXTURES=true, and a database name containing 'test'.",
        pytrace=False,
    )


@pytest_asyncio.fixture(autouse=True)
async def dispose_database_pool_between_tests():
    # Rate-limit counters are intentionally shared by real requests, but
    # must not leak from one isolated test into the next test in the same
    # database group. Keep each test's in-request sequence intact.
    async with SessionFactory() as session:
        await session.execute(text("delete from security_events where type = 'api_rate_limit'"))
        await session.execute(text("delete from login_attempts where detail is not null"))
        await session.commit()
    yield
    await engine.dispose()
