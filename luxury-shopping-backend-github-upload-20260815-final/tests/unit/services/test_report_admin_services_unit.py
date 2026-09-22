from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from backend.app.services import report_admin_services as ras


def test_pdf_arabic_font_registration_skips_missing_candidate(monkeypatch, tmp_path) -> None:
    packaged_font = ras.BACKEND_DIR / "assets" / "fonts" / "Tajawal-Regular.ttf"
    assert packaged_font.is_file()
    monkeypatch.setattr(
        ras,
        "_pdf_arabic_font_candidates",
        lambda: (tmp_path / "missing.ttf", packaged_font),
    )

    assert ras._register_pdf_arabic_font(pdfmetrics, TTFont) == "ArabicReportFont1"


@pytest.mark.parametrize(
    "report_type",
    ("summary", "sales", "orders", "revenue", "customers", "merchant_revenue"),
)
def test_pdf_reports_use_arabic_font_and_brand_identity(report_type: str) -> None:
    logo = ras.BACKEND_DIR / "assets" / "branding" / "luxury-shopping-logo.png"
    assert logo.is_file()
    metadata = {
        "date_basis": "orders.created_at plus successful payment/refund status",
        "order_count": "3",
        "eligible_order_count": "3",
        "gross_revenue": "140042.25",
        "paid_amount": "120000.00",
        "refund_amount": "0.00",
        "net_revenue": "140042.25",
        "currency_code": "YER",
        "partner_scope": None,
    }

    pdf = ras.ReportGenerationService._render_pdf(report_type, [], ("metric", "value"), metadata)

    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 10_000
    assert b"/Subtype /Image" in pdf
    assert b"/Title" in pdf


def test_pdf_export_completes_when_brand_logo_is_unavailable(monkeypatch, tmp_path) -> None:
    packaged_font = ras.BACKEND_DIR / "assets" / "fonts" / "Tajawal-Regular.ttf"
    metadata = {
        "date_basis": "orders.created_at plus successful payment/refund status",
        "order_count": "1",
        "eligible_order_count": "1",
        "gross_revenue": "100.00",
        "paid_amount": "100.00",
        "refund_amount": "0.00",
        "net_revenue": "100.00",
        "currency_code": "YER",
        "partner_scope": None,
    }
    monkeypatch.setattr(ras, "BACKEND_DIR", tmp_path)
    monkeypatch.setattr(ras, "_pdf_arabic_font_candidates", lambda: (packaged_font,))

    pdf = ras.ReportGenerationService._render_pdf("summary", [], ("metric", "value"), metadata)

    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 5_000
    assert b"/Title" in pdf


@pytest.mark.asyncio
async def test_order_activity_summary_includes_pending_and_supplemental_orders(monkeypatch) -> None:
    pending_order = SimpleNamespace(total="125.00", currency_code="YER")
    local_request = {"amount": "75.00"}

    monkeypatch.setattr(
        ras.RevenueRecognitionService,
        "eligible_orders",
        AsyncMock(return_value=[pending_order]),
    )
    monkeypatch.setattr(
        ras.RevenueRecognitionService,
        "_supplemental_orders",
        AsyncMock(return_value=[("local_shopping_requests", local_request)]),
    )
    monkeypatch.setattr(ras, "serialize_record", lambda record: record)

    result = await ras.RevenueRecognitionService.order_activity_summary(object())

    assert result == {
        "order_count": 2,
        "order_value": ras.money("200.00"),
        "currency_code": "YER",
    }
