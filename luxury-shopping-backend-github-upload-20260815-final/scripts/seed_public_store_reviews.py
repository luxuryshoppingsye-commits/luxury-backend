from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.backup_postgres import load_env_files, to_async_url  # noqa: E402
from backend.app.security.passwords import hash_password  # noqa: E402

SEED_VERSION = "public-store-review-v1"
SEED_NAMESPACE = uuid.UUID("8f4e2c1a-9b3d-4f6e-a5c7-2d8e1f0a3b4c")
INTERNAL_EMAIL_DOMAIN = "reviews-seed.internal.luxury"

PUBLIC_STORE_REVIEWS: tuple[dict[str, Any], ...] = (
    {
        "slug": "noura-alotaibi",
        "customer_name": "نورة العتيبي",
        "rating": 5,
        "comment": "تجربة رائعة من أول طلب. التغليف أنيق والتوصيل أسرع من المتوقع، والمنتج مطابق للصور.",
        "days_ago": 4,
    },
    {
        "slug": "fahad-aldosari",
        "customer_name": "فهد الدوسري",
        "rating": 5,
        "comment": "خدمة عملاء متجاوبة وخيارات دفع مريحة. سأكرر الشراء بالتأكيد.",
        "days_ago": 9,
    },
    {
        "slug": "sarah-alqahtani",
        "customer_name": "سارة القحطاني",
        "rating": 4,
        "comment": "المنتجات فاخرة والأسعار مناسبة للجودة. أتمنى إضافة المزيد من العطور النسائية.",
        "days_ago": 14,
    },
    {
        "slug": "abdullah-alshammari",
        "customer_name": "عبدالله الشمري",
        "rating": 5,
        "comment": "طلبت ساعة هدية ووصلت بحالة ممتازة مع بطاقة تهنئة. تجربة تليق بمتجر فاخر.",
        "days_ago": 18,
    },
    {
        "slug": "reem-alharbi",
        "customer_name": "ريم الحربي",
        "rating": 4,
        "comment": "تجربة تسوق سلسة من الموقع وحتى الاستلام. التقييم 4 فقط لأن الموعد تأخر يوماً واحداً.",
        "days_ago": 22,
    },
    {
        "slug": "mohammed-alzahrani",
        "customer_name": "محمد الزهراني",
        "rating": 5,
        "comment": "أفضل متجر تعاملت معه مؤخراً: جودة، التزام، ومتابعة واضحة لحالة الطلب.",
        "days_ago": 27,
    },
)


def _seed_uuid(kind: str, slug: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, f"{SEED_VERSION}:{kind}:{slug}")


def _comment_hash(customer_name: str, comment: str) -> str:
    payload = f"{customer_name.strip()}|{comment.strip()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _database_name_from_url(database_url: str) -> str:
    return urlsplit(database_url).path.lstrip("/")


def _database_is_test(database_name: str) -> bool:
    name = database_name.lower()
    return name == "luxury_test" or name.endswith("_test") or "_e2e_test" in name


def _database_is_recovery_or_test(database_name: str) -> bool:
    name = database_name.lower()
    return _database_is_test(name) or "recovery" in name or name.endswith("_qa")


def _fixtures_flag_enabled() -> bool:
    return os.environ.get("ALLOW_TEST_FIXTURES", "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_seed_permitted(*, confirm: bool, allow_production_seed: bool, database_url: str) -> dict[str, str]:
    if not confirm and not _fixtures_flag_enabled():
        raise SystemExit(
            "Refusing seed: pass --confirm or set ALLOW_TEST_FIXTURES=true (test DB only unless --allow-production-seed)."
        )

    db_name = _database_name_from_url(database_url)
    host = (urlsplit(database_url).hostname or "").lower()
    info = {"database": db_name, "host": host}

    if allow_production_seed:
        if not confirm:
            raise SystemExit("Refusing production seed: --allow-production-seed requires --confirm.")
        print(
            f"WARNING: seeding operational database '{db_name}' on host '{host}' "
            f"with approved public store reviews ({SEED_VERSION})."
        )
        return info

    if not _database_is_recovery_or_test(db_name):
        raise SystemExit(
            f"Refusing seed on database '{db_name}'. "
            "Use a test/recovery database or pass --allow-production-seed with --confirm for Neon/production."
        )

    if _fixtures_flag_enabled() and os.environ.get("APP_ENV", "").strip().lower() == "test":
        if not _database_is_test(db_name):
            raise SystemExit("ALLOW_TEST_FIXTURES with APP_ENV=test requires a trusted test database name.")
    elif not confirm:
        raise SystemExit("Refusing seed: non-test database requires --confirm when ALLOW_TEST_FIXTURES is not set.")

    return info


async def _table_columns(session: AsyncSession, table: str) -> set[str]:
    result = await session.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table
            """
        ),
        {"table": table},
    )
    return {str(row[0]) for row in result.all()}


async def _ensure_seed_user(session: AsyncSession, *, user_id: uuid.UUID, full_name: str, slug: str) -> None:
    email = f"{SEED_VERSION}+{slug}@{INTERNAL_EMAIL_DOMAIN}"
    password_hash = hash_password(f"{SEED_VERSION}:{slug}:disabled")
    await session.execute(
        text(
            """
            INSERT INTO public.users (id, email, password_hash, is_active, created_at, updated_at)
            VALUES (:id, :email, :password_hash, false, now(), now())
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"id": user_id, "email": email, "password_hash": password_hash},
    )

    profile_columns = await _table_columns(session, "profiles")
    if not profile_columns:
        return
    profile_id = user_id
    profile_values: dict[str, Any] = {
        "id": profile_id,
        "user_id": user_id,
        "full_name": full_name,
        "email": email,
    }
    if "city" in profile_columns:
        profile_values["city"] = "الرياض"
    insert_cols = [key for key in profile_values if key in profile_columns]
    placeholders = ", ".join(f":{col}" for col in insert_cols)
    col_list = ", ".join(insert_cols)
    await session.execute(
        text(
            f"""
            INSERT INTO public.profiles ({col_list})
            VALUES ({placeholders})
            ON CONFLICT (id) DO UPDATE SET full_name = EXCLUDED.full_name
            """
        ),
        {key: profile_values[key] for key in insert_cols},
    )


async def _upsert_handover_review(
    session: AsyncSession,
    *,
    review_id: uuid.UUID,
    user_id: uuid.UUID,
    item: dict[str, Any],
    created_at: datetime,
) -> str:
    marker = f"LSH_SEED:{SEED_VERSION}:{item['slug']}"
    await session.execute(
        text(
            """
            INSERT INTO public.store_reviews (
                id, user_id, rating, comment, customer_name,
                is_approved, is_rejected, admin_notes, created_at, updated_at
            )
            VALUES (
                :id, :user_id, :rating, :comment, :customer_name,
                true, false, :admin_notes, :created_at, :updated_at
            )
            ON CONFLICT (user_id) DO UPDATE SET
                rating = EXCLUDED.rating,
                comment = EXCLUDED.comment,
                customer_name = EXCLUDED.customer_name,
                is_approved = true,
                is_rejected = false,
                admin_notes = EXCLUDED.admin_notes,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "id": review_id,
            "user_id": user_id,
            "rating": item["rating"],
            "comment": item["comment"],
            "customer_name": item["customer_name"],
            "admin_notes": marker,
            "created_at": created_at,
            "updated_at": created_at,
        },
    )
    return "handover"


async def _upsert_generic_review(
    session: AsyncSession,
    columns: set[str],
    *,
    review_id: uuid.UUID,
    user_id: uuid.UUID,
    item: dict[str, Any],
    created_at: datetime,
) -> str:
    marker = f"LSH_SEED:{SEED_VERSION}:{item['slug']}"
    payload: dict[str, Any] = {
        "id": review_id,
        "user_id": user_id,
        "rating": item["rating"],
        "created_at": created_at,
        "updated_at": created_at,
    }
    if "comment" in columns:
        payload["comment"] = item["comment"]
    if "customer_name" in columns:
        payload["customer_name"] = item["customer_name"]
    if "status" in columns:
        payload["status"] = "approved"
    if "title" in columns:
        payload["title"] = item["customer_name"]
    if "body" in columns:
        payload["body"] = item["comment"]
    if "is_approved" in columns:
        payload["is_approved"] = True
    if "is_rejected" in columns:
        payload["is_rejected"] = False
    if "admin_notes" in columns:
        payload["admin_notes"] = marker

    insert_cols = [key for key in payload if key in columns]
    col_list = ", ".join(insert_cols)
    placeholders = ", ".join(f":{key}" for key in insert_cols)
    updates = ", ".join(
        f"{col} = EXCLUDED.{col}"
        for col in insert_cols
        if col not in {"id", "created_at"}
    )
    await session.execute(
        text(
            f"""
            INSERT INTO public.store_reviews ({col_list})
            VALUES ({placeholders})
            ON CONFLICT (id) DO UPDATE SET {updates}
            """
        ),
        {key: payload[key] for key in insert_cols},
    )
    return "generic"


async def seed_public_store_reviews(session: AsyncSession) -> dict[str, Any]:
    review_columns = await _table_columns(session, "store_reviews")
    if not review_columns:
        raise SystemExit("public.store_reviews table not found.")

    use_handover = "is_approved" in review_columns and "customer_name" in review_columns
    now = datetime.now(timezone.utc)
    seeded: list[dict[str, Any]] = []

    for item in PUBLIC_STORE_REVIEWS:
        review_id = _seed_uuid("review", item["slug"])
        user_id = _seed_uuid("user", item["slug"])
        created_at = now - timedelta(days=int(item["days_ago"]))
        await _ensure_seed_user(session, user_id=user_id, full_name=item["customer_name"], slug=item["slug"])

        if use_handover:
            schema = await _upsert_handover_review(
                session,
                review_id=review_id,
                user_id=user_id,
                item=item,
                created_at=created_at,
            )
        else:
            schema = await _upsert_generic_review(
                session,
                review_columns,
                review_id=review_id,
                user_id=user_id,
                item=item,
                created_at=created_at,
            )

        seeded.append(
            {
                "slug": item["slug"],
                "id": str(review_id),
                "customer_name": item["customer_name"],
                "rating": item["rating"],
                "comment_hash": _comment_hash(item["customer_name"], item["comment"]),
                "schema": schema,
            }
        )

    return {"seed_version": SEED_VERSION, "count": len(seeded), "reviews": seeded}


async def _run(args: argparse.Namespace) -> int:
    load_env_files()
    database_url = args.database_url or os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required (env or --database-url).")

    target = _assert_seed_permitted(
        confirm=args.confirm,
        allow_production_seed=args.allow_production_seed,
        database_url=database_url,
    )

    if args.dry_run:
        print(
            {
                "dry_run": True,
                "target": target,
                "would_seed": len(PUBLIC_STORE_REVIEWS),
                "names": [item["customer_name"] for item in PUBLIC_STORE_REVIEWS],
            }
        )
        return 0

    engine = create_async_engine(to_async_url(database_url), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            async with session.begin():
                summary = await seed_public_store_reviews(session)
        print(summary)
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently seed approved public store reviews with natural Saudi Arabic names. "
            "Requires --confirm or ALLOW_TEST_FIXTURES=true. "
            "Production/Neon requires --allow-production-seed --confirm."
        )
    )
    parser.add_argument("--database-url", default="", help="Override DATABASE_URL from environment.")
    parser.add_argument("--confirm", action="store_true", help="Acknowledge intentional database writes.")
    parser.add_argument(
        "--allow-production-seed",
        action="store_true",
        help="Allow seeding operational databases (e.g. Neon production). Requires --confirm.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate guards and print planned rows only.")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
