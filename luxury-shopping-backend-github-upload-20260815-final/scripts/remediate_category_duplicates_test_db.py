from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text

from app.config import get_settings
from app.database import engine


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "artifacts" / "database-integrity-remediation" / "category-duplicate-remediation.json"


def _assert_safe_settings() -> None:
    settings = get_settings()
    parsed = urlsplit(settings.database_url)
    if settings.app_env != "test":
        raise SystemExit("Refusing remediation outside APP_ENV=test.")
    if not settings.allow_test_fixtures:
        raise SystemExit("Refusing remediation when ALLOW_TEST_FIXTURES is not true.")
    if settings.database_name != "luxury_full_cross_platform_e2e_test":
        raise SystemExit("Refusing remediation outside luxury_full_cross_platform_e2e_test.")
    if parsed.hostname != "127.0.0.1" or parsed.port != 55433:
        raise SystemExit("Refusing remediation outside 127.0.0.1:55433.")
    if settings.database_name == "luxury_official_recovery":
        raise SystemExit("Refusing remediation on recovery database.")


async def _database_info(connection) -> dict[str, object]:
    row = (
        await connection.execute(
            text("select current_database() as database, inet_server_port() as port, current_user as user_name")
        )
    ).mappings().one()
    if row["database"] != "luxury_full_cross_platform_e2e_test" or row["port"] != 55433:
        raise SystemExit("Runtime database safety check failed.")
    return {"database": row["database"], "port": row["port"], "user_name": row["user_name"]}


async def _duplicate_groups(connection) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in (
            await connection.execute(
                text(
                    """
                    select lower(btrim(name)) as normalized_name, count(*) as count
                    from categories
                    where deleted_at is null
                    group by lower(btrim(name))
                    having count(*) > 1
                    order by count desc, normalized_name
                    """
                )
            )
        ).mappings().all()
    ]


async def main() -> int:
    _assert_safe_settings()
    run_id = f"category-dedupe-{uuid.uuid4().hex[:10]}"
    started_at = datetime.now(timezone.utc)
    remediated: list[dict[str, object]] = []
    async with engine.begin() as connection:
        database = await _database_info(connection)
        before = await _duplicate_groups(connection)
        for group in before:
            rows = (
                await connection.execute(
                    text(
                        """
                        select
                            c.id,
                            c.name,
                            c.slug,
                            c.created_at,
                            count(p.id) as product_count
                        from categories c
                        left join products p on p.category_id = c.id and p.deleted_at is null
                        where c.deleted_at is null
                          and lower(btrim(c.name)) = :normalized_name
                        group by c.id, c.name, c.slug, c.created_at
                        order by count(p.id) desc, c.created_at nulls last, c.id
                        """
                    ),
                    {"normalized_name": group["normalized_name"]},
                )
            ).mappings().all()
            if len(rows) < 2:
                continue
            keeper = rows[0]
            merged_rows: list[dict[str, object]] = []
            for duplicate in rows[1:]:
                product_rows = (
                    await connection.execute(
                        text(
                            """
                            update products
                            set category_id = :keeper_id, updated_at = now()
                            where category_id = :duplicate_id
                            returning id::text
                            """
                        ),
                        {"keeper_id": keeper["id"], "duplicate_id": duplicate["id"]},
                    )
                ).scalars().all()
                await connection.execute(
                    text(
                        """
                        update categories
                        set
                            is_active = false,
                            deleted_at = :deleted_at,
                            updated_at = :deleted_at,
                            extra_data = coalesce(extra_data, '{}'::jsonb)
                                || jsonb_build_object(
                                    'merged_into_category_id', cast(:keeper_id_text as text),
                                    'remediation', 'duplicate_category_name',
                                    'remediation_run_id', cast(:run_id as text)
                                )
                        where id = :duplicate_id
                        """
                    ),
                    {
                        "deleted_at": started_at,
                        "keeper_id_text": str(keeper["id"]),
                        "run_id": run_id,
                        "duplicate_id": duplicate["id"],
                    },
                )
                merged_rows.append(
                    {
                        "duplicate_category_id": str(duplicate["id"]),
                        "duplicate_name": duplicate["name"],
                        "moved_product_ids": product_rows,
                        "moved_product_count": len(product_rows),
                    }
                )
            remediated.append(
                {
                    "normalized_name": group["normalized_name"],
                    "keeper_category_id": str(keeper["id"]),
                    "keeper_product_count_before": int(keeper["product_count"]),
                    "merged": merged_rows,
                }
            )
        after = await _duplicate_groups(connection)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "started_at": started_at.isoformat(),
                "database": database,
                "before_duplicate_groups": before,
                "after_duplicate_groups": after,
                "remediated_groups": remediated,
                "physical_deletes": 0,
                "secret_values_printed": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"artifact": str(ARTIFACT), "before": len(before), "after": len(after)}, ensure_ascii=False))
    return 0 if not after else 1


if __name__ == "__main__":
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("ALLOW_TEST_FIXTURES", "true")
    raise SystemExit(asyncio.run(main()))
