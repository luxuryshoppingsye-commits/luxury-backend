from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.api.routes import operations
from backend.app.services.report_admin_services import (
    _validate_theme_setting_value,
    _validate_theme_settings_collection,
)


def test_theme_validator_accepts_every_value_emitted_by_design_panels() -> None:
    settings = {
        "colors": {
            "primary": "43 85% 50%",
            "background": "0 0% 100%",
            "foreground": "220 14% 10%",
            "card": "#FFFFFF",
            "gold": "43 85% 50%",
        },
        "typography": {"fontFamily": "Tajawal", "headingSize": "1.5", "bodySize": "1"},
        "layout": {
            "containerWidth": "1400px",
            "sectionPadding": "6rem",
            "headerStyle": "fixed",
            "borderRadius": "0.75rem",
        },
        "components": {
            "cardRadius": "1",
            "cardShadow": "elegant",
            "cardHover": True,
            "buttonRadius": "0.5",
            "buttonSize": "default",
            "inputRadius": "0.5",
        },
        "animations": {"enabled": True, "duration": "0.3s", "type": "fade"},
        "hero": {
            "title": "عالم الفخامة",
            "subtitle": "اكتشف أرقى المنتجات الفاخرة",
            "imageUrl": "https://images.example.com/hero.webp",
            "showCta": True,
        },
    }

    _validate_theme_settings_collection(settings)


@pytest.mark.parametrize(
    ("setting_key", "value", "field"),
    [
        ("colors", {"primary": "not-a-color"}, "primary"),
        ("typography", {"fontFamily": "Broken Font"}, "fontFamily"),
        ("typography", {"headingSize": "200rem"}, "headingSize"),
        ("layout", {"containerWidth": "99999px"}, "containerWidth"),
        ("layout", {"headerStyle": "floating"}, "headerStyle"),
        ("cards", {"borderRadius": "40rem"}, "borderRadius"),
        ("buttons", {"size": "giant"}, "size"),
        ("animations", {"duration": "20s"}, "duration"),
        ("hero", {"imageUrl": "javascript:alert(1)"}, "imageUrl"),
        ("default", {"headingSize": "200rem"}, "headingSize"),
    ],
)
def test_theme_validator_rejects_values_that_can_break_storefront(
    setting_key: str,
    value: dict[str, object],
    field: str,
) -> None:
    with pytest.raises(HTTPException) as caught:
        _validate_theme_setting_value(setting_key, value)

    assert caught.value.status_code == 422
    assert caught.value.detail == f"invalid_theme_setting:{setting_key}:{field}" or caught.value.detail == "invalid_theme_payload"


def test_theme_validator_keeps_legacy_default_payload_compatible() -> None:
    _validate_theme_setting_value(
        "default",
        {
            "primary": "#976817",
            "buttonRadius": 8,
            "fontScale": 1.0,
            "colors": {"primary": "43 85% 50%"},
        },
    )


def test_theme_preview_rejects_invalid_component_before_it_can_be_saved() -> None:
    with pytest.raises(HTTPException) as caught:
        _validate_theme_settings_collection(
            {"components": {"cardRadius": "100", "buttonSize": "default"}}
        )

    assert caught.value.detail == "invalid_theme_setting:cards:borderRadius"


class _ThemeSession:
    def __init__(self, template: SimpleNamespace, active_template: SimpleNamespace) -> None:
        self.template = template
        self.active_template = active_template
        self.commit_count = 0
        self.rollback_count = 0

    async def get(self, model: object, record_id: uuid.UUID) -> SimpleNamespace:
        return self.template

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1

    def add(self, row: object) -> None:
        raise AssertionError("The active template row already exists in this test")


class _JsonRequest:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def json(self) -> dict[str, object]:
        return self.payload


@pytest.mark.asyncio
async def test_apply_template_splits_legacy_components_and_commits_once(monkeypatch: pytest.MonkeyPatch) -> None:
    template_id = uuid.uuid4()
    template = SimpleNamespace(
        extra_data={
            "settings": {
                "components": {
                    "cardRadius": "1rem",
                    "cardShadow": "elegant",
                    "cardHover": True,
                    "buttonRadius": "0.5rem",
                    "buttonSize": "large",
                    "inputRadius": "0.25rem",
                }
            }
        }
    )
    active_template = SimpleNamespace(extra_data={})
    session = _ThemeSession(template, active_template)
    saved: dict[str, tuple[dict[str, object], bool]] = {}

    class FakeThemeService:
        @staticmethod
        def require_access(roles: set[str]) -> None:
            assert roles == {"admin"}

        async def save(self, session: object, **kwargs: object) -> None:
            saved[str(kwargs["setting_key"])] = (kwargs["body"]["value"], kwargs["commit"])

    async def fake_rows(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return [active_template]

    monkeypatch.setattr(operations, "ThemeAdminService", FakeThemeService)
    monkeypatch.setattr(operations, "_rows", fake_rows)
    monkeypatch.setattr(operations, "serialize_record", lambda row: row.extra_data)

    await operations.api_content_apply_theme_template(
        template_id,
        staff=SimpleNamespace(id=uuid.uuid4()),
        roles={"admin"},
        session=session,
    )

    assert saved == {
        "cards": (
            {"borderRadius": "1rem", "shadow": "elegant", "hoverEffect": True},
            False,
        ),
        "buttons": ({"borderRadius": "0.5rem", "size": "large"}, False),
        "inputs": ({"borderRadius": "0.25rem"}, False),
    }
    assert session.commit_count == 1
    assert session.rollback_count == 0


@pytest.mark.asyncio
async def test_apply_template_rolls_back_every_section_when_one_save_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    template = SimpleNamespace(
        extra_data={
            "settings": {
                "colors": {"primary": "43 85% 50%"},
                "typography": {"fontFamily": "Tajawal"},
            }
        }
    )
    active_template = SimpleNamespace(extra_data={})
    session = _ThemeSession(template, active_template)

    class FailingThemeService:
        @staticmethod
        def require_access(roles: set[str]) -> None:
            return None

        async def save(self, session: object, **kwargs: object) -> None:
            if kwargs["setting_key"] == "typography":
                raise RuntimeError("save_failed")

    async def fake_rows(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return [active_template]

    monkeypatch.setattr(operations, "ThemeAdminService", FailingThemeService)
    monkeypatch.setattr(operations, "_rows", fake_rows)

    with pytest.raises(RuntimeError, match="save_failed"):
        await operations.api_content_apply_theme_template(
            uuid.uuid4(),
            staff=SimpleNamespace(id=uuid.uuid4()),
            roles={"admin"},
            session=session,
        )

    assert session.commit_count == 0
    assert session.rollback_count == 1


@pytest.mark.asyncio
async def test_manual_history_request_reuses_the_automatic_save_history(monkeypatch: pytest.MonkeyPatch) -> None:
    staff_id = uuid.uuid4()
    automatic_history = SimpleNamespace(
        id=uuid.uuid4(),
        extra_data={
            "setting_key": "colors",
            "new_value": {"primary": "43 85% 50%"},
            "updated_by": str(staff_id),
            "description": "Theme colors version 2",
        },
    )
    session = _ThemeSession(SimpleNamespace(), SimpleNamespace())

    async def fake_rows(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return [automatic_history]

    async def unexpected_create(*args: object, **kwargs: object) -> object:
        raise AssertionError("A duplicate history row must not be created")

    monkeypatch.setattr(operations, "_rows", fake_rows)
    monkeypatch.setattr(operations, "_api_create", unexpected_create)
    monkeypatch.setattr(
        operations,
        "serialize_record",
        lambda row: {"id": str(row.id), **row.extra_data},
    )

    result = await operations.api_content_create_theme_history(
        request=_JsonRequest(
            {
                "setting_key": "colors",
                "old_value": {"primary": "40 80% 50%"},
                "new_value": {"primary": "43 85% 50%"},
                "description": "تحديث ألوان المتجر",
            }
        ),
        staff=SimpleNamespace(id=staff_id),
        roles={"admin"},
        session=session,
    )

    assert session.commit_count == 1
    assert automatic_history.extra_data["description"] == "تحديث ألوان المتجر"
    assert result["data"]["id"] == str(automatic_history.id)
