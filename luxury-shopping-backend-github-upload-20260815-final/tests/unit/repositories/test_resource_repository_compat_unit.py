from __future__ import annotations

from backend.app.models import MODEL_BY_TABLE
from backend.app.repositories.resources import ResourceRepository, serialize_record


def _repository(table: str) -> ResourceRepository:
    return ResourceRepository(session=object(), table=table, user_id=None, roles=set())


def test_public_static_page_legacy_boolean_filter_uses_canonical_column() -> None:
    clause = _repository("static_pages")._filter_clause(
        {"column": "is_published", "operator": "eq", "value": True}
    )

    assert "static_pages.is_active" in str(clause)
    assert "extra_data" not in str(clause)


def test_public_page_section_legacy_boolean_filter_uses_canonical_column() -> None:
    clause = _repository("page_sections")._filter_clause(
        {"column": "is_visible", "operator": "eq", "value": True}
    )

    assert "page_sections.is_active" in str(clause)
    assert "extra_data" not in str(clause)


def test_blog_article_legacy_boolean_filter_uses_canonical_column() -> None:
    clause = _repository("blog_articles")._filter_clause(
        {"column": "is_published", "operator": "eq", "value": True}
    )

    assert "blog_articles.is_active" in str(clause)
    assert "extra_data" not in str(clause)


def test_product_review_approval_filter_uses_canonical_column() -> None:
    clause = _repository("product_reviews")._filter_clause(
        {"column": "is_approved", "operator": "eq", "value": True}
    )

    assert "product_reviews.is_approved" in str(clause)
    assert "extra_data" not in str(clause)
    assert str(clause.right).lower() == "true"


def test_static_page_serialization_returns_legacy_content_aliases() -> None:
    model = MODEL_BY_TABLE["static_pages"]
    row = serialize_record(
        model(title="Terms", slug="terms", body="Terms body", is_active=True)
    )

    assert row["body"] == "Terms body"
    assert row["content"] == "Terms body"
    assert row["is_active"] is True
    assert row["is_published"] is True


def test_blog_article_serialization_returns_legacy_content_aliases() -> None:
    model = MODEL_BY_TABLE["blog_articles"]
    row = serialize_record(
        model(
            title="Article",
            slug="article",
            body="Article body",
            image_url="/uploads/article.png",
            is_active=True,
        )
    )

    assert row["content"] == "Article body"
    assert row["cover_image"] == "/uploads/article.png"
    assert row["is_published"] is True
