from backend.app.api.routes.operations import _normalize_store_review_status_payload
import asyncio
import uuid

from backend.app.services.store_review_compat import (
    _GENERIC_PUBLIC_SQL,
    create_handover_store_review,
    normalize_store_review_row,
    update_handover_store_review,
    update_handover_store_review_status,
    mask_store_review_name,
)


def test_admin_approval_payload_also_updates_legacy_status_column() -> None:
    payload = _normalize_store_review_status_payload(
        {"is_approved": True, "is_rejected": False}
    )

    assert payload["status"] == "approved"
    assert payload["is_approved"] is True


def test_generic_public_query_supports_resource_extra_data_schema() -> None:
    assert "sr.extra_data ->> 'rating'" in _GENERIC_PUBLIC_SQL
    assert "sr.extra_data ->> 'is_approved'" in _GENERIC_PUBLIC_SQL
    assert "'accepted'" in _GENERIC_PUBLIC_SQL


def test_accepted_review_status_is_normalized_as_public() -> None:
    review = normalize_store_review_row(
        {
            "id": "review-1",
            "user_id": "user-1",
            "rating": "5",
            "status": "accepted",
            "created_at": "2026-08-26T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
        }
    )

    assert review["is_approved"] is True
    assert review["is_rejected"] is False


def test_handover_create_uses_direct_review_columns() -> None:
    class Mappings:
        def one(self):
            return {
                "id": uuid.uuid4(),
                "user_id": uuid.uuid4(),
                "rating": 5,
                "comment": "تجربة موفقة",
                "customer_name": "عميل",
                "status": "pending",
                "is_approved": False,
                "is_rejected": False,
                "admin_notes": None,
                "created_at": None,
                "updated_at": None,
            }

    class Result:
        def mappings(self):
            return Mappings()

    class Session:
        statement = ""
        params: dict[str, object] = {}

        async def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params
            return Result()

    session = Session()
    result = asyncio.run(
        create_handover_store_review(
            session,
            user_id=uuid.uuid4(),
            rating=5,
            comment="تجربة موفقة",
            customer_name="عميل",
        )
    )

    assert "INSERT INTO public.store_reviews" in session.statement
    assert "rating, comment, customer_name" in session.statement
    assert "ON CONFLICT (user_id) DO NOTHING" in session.statement
    assert "'pending', FALSE, FALSE" in session.statement
    assert "extra_data" not in session.statement
    assert session.params["rating"] == 5
    assert result is not None
    assert result["rating"] == 5


def test_hidden_store_review_name_keeps_only_a_safe_preview() -> None:
    review = normalize_store_review_row(
        {
            "id": "review-1",
            "user_id": "user-1",
            "rating": 5,
            "comment": "تجربة موفقة",
            "customer_name": "سمية احمد",
            "show_name": False,
        }
    )

    assert review["customer_name"] == "سمية***د"
    assert review["show_name"] is False
    assert mask_store_review_name("سمية احمد") == "سمية***د"


def test_handover_status_update_sets_visible_approval_fields() -> None:
    review_id = uuid.uuid4()

    class Mappings:
        def one_or_none(self):
            return {
                "id": review_id,
                "user_id": uuid.uuid4(),
                "rating": 5,
                "comment": "تجربة موفقة",
                "customer_name": "عميل",
                "status": "approved",
                "is_approved": True,
                "is_rejected": False,
                "admin_notes": None,
                "created_at": None,
                "updated_at": None,
            }

    class Result:
        def mappings(self):
            return Mappings()

    class Session:
        statement = ""
        params: dict[str, object] = {}

        async def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params
            return Result()

    session = Session()
    result = asyncio.run(
        update_handover_store_review_status(
            session,
            review_id=review_id,
            status="approved",
            is_approved=True,
            is_rejected=False,
            admin_notes=None,
        )
    )

    assert "UPDATE public.store_reviews" in session.statement
    assert "is_approved = :is_approved" in session.statement
    assert session.params["review_id"] == review_id
    assert result is not None
    assert result["status"] == "approved"
    assert result["is_approved"] is True


def test_handover_content_update_is_scoped_to_the_review_owner() -> None:
    review_id = uuid.uuid4()
    user_id = uuid.uuid4()

    class Mappings:
        def one_or_none(self):
            return {
                "id": review_id,
                "user_id": user_id,
                "rating": 4,
                "comment": "تجربة محدثة",
                "customer_name": "عميل",
                "status": "pending",
                "is_approved": False,
                "is_rejected": False,
                "admin_notes": None,
                "created_at": None,
                "updated_at": None,
            }

    class Result:
        def mappings(self):
            return Mappings()

    class Session:
        statement = ""
        params: dict[str, object] = {}

        async def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params
            return Result()

    session = Session()
    result = asyncio.run(
        update_handover_store_review(
            session,
            review_id=review_id,
            user_id=user_id,
            rating=4,
            comment="تجربة محدثة",
            customer_name="عميل",
        )
    )

    assert "UPDATE public.store_reviews" in session.statement
    assert "AND user_id = :user_id" in session.statement
    assert "status = 'pending'" in session.statement
    assert session.params["review_id"] == review_id
    assert session.params["user_id"] == user_id
    assert result is not None
    assert result["rating"] == 4
