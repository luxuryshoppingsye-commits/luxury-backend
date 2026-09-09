from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from fastapi import HTTPException
from backend.app.api.routes import auth, operations
from backend.app.services import function_service, notification_service as ns


def result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value)


def session_with(*rows):
    return SimpleNamespace(execute=AsyncMock(side_effect=[result(r) for r in rows]),
        add=lambda row: added.append(row), get=AsyncMock(return_value=None),
        commit=AsyncMock(), flush=AsyncMock())


added = []


@pytest.fixture(autouse=True)
def clean_added():
    added.clear()


@pytest.mark.asyncio
async def test_application_keeps_selected_categories_description_and_shop_address(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), email='applicant@example.test')
    profile = SimpleNamespace(phone='777123456', city='Customer city')
    session = session_with(profile, None, None)
    monkeypatch.setattr(auth, 'account_security_for', AsyncMock(return_value=SimpleNamespace(account_status='active')))
    monkeypatch.setattr(auth, 'record_security_event', AsyncMock())
    monkeypatch.setattr(auth, 'auth_payload', AsyncMock(return_value={'roles': ['customer']}))
    body = {'storeName': 'متجر الاختبار', 'businessType': 'fashion',
        'storeCategories': ['beauty', 'accessories', 'beauty'],
        'description': '  منتجات التجميل والإكسسوارات  ',
        'storeCity': '  صنعاء  ', 'storeAddress': '  شارع المتجر  '}
    payload = await auth.register_merchant(SimpleNamespace(json=AsyncMock(return_value=body)), user, session)
    application = added[0]
    assert application.description == 'منتجات التجميل والإكسسوارات'
    assert application.extra_data['store_categories'] == ['beauty', 'accessories']
    assert application.extra_data['store_city'] == 'صنعاء'
    assert application.extra_data['store_address'] == 'شارع المتجر'
    assert profile.city == 'Customer city'
    assert payload['requires_review'] is True
    assert payload['merchant_portal_enabled'] is False
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('body,detail', [
    ({'storeCategories': ['unknown']}, 'invalid_store_category'),
    ({'storeCategories': 'fashion'}, 'store_categories_must_be_a_list'),
    ({'businessType': 'invalid', 'storeCategories': ['beauty']}, 'invalid_business_type'),
    ({'storeCategories': [], 'businessType': 'other'}, 'store_category_required'),
    ({'storeCity': 'x' * 121}, 'store_details_too_long'),
])
async def test_invalid_store_settings_do_not_write_application(body, detail):
    request = SimpleNamespace(json=AsyncMock(return_value={'storeName': 'Store', **body}))
    session = session_with()
    with pytest.raises(HTTPException) as error:
        await auth.register_merchant(request, SimpleNamespace(id=uuid.uuid4()), session)
    assert error.value.detail == detail
    assert added == []
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['review', 'function'])
async def test_both_approval_routes_copy_settings_and_queue_phone_alert(monkeypatch, entrypoint):
    user_id = uuid.uuid4()
    application = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, name='Store',
        email='applicant@example.test', phone='777123456', description='Store details',
        logo_url=None, status='pending', extra_data={'business_type': 'beauty',
        'store_categories': ['beauty', 'accessories'], 'store_city': 'Shop city',
        'store_address': 'Shop street'}, __table__=SimpleNamespace(c={'extra_data'}))
    session = session_with(application, None)
    notifications = SimpleNamespace(create_notification=AsyncMock())
    module = operations if entrypoint == 'review' else function_service
    monkeypatch.setattr(module, 'NotificationService', lambda _: notifications)
    monkeypatch.setattr(module, 'serialize_record', lambda row: {'id': str(row.id)})
    actor = SimpleNamespace(id=uuid.uuid4())
    if entrypoint == 'review':
        await operations.review_partner_application(application.id,
            SimpleNamespace(json=AsyncMock(return_value={'status': 'approved'})), actor, session)
    else:
        await function_service._approve_partner(session, {'application_id': str(application.id)}, actor)
    storefront = next(r for r in added if r.__tablename__ == 'partner_storefronts')
    assert storefront.extra_data['store_categories'] == ['beauty', 'accessories']
    assert storefront.extra_data['store_city'] == 'Shop city'
    assert storefront.extra_data['store_address'] == 'Shop street'
    assert storefront.description == 'Store details'
    payload = notifications.create_notification.await_args.args[0]
    assert payload.notification_type == 'partner_application_approved'
    assert 'mobile_push' in payload.delivery_channels
    assert payload.priority == 'high'
    assert payload.payload['deep_link'] == '/partner/agreement?required=1'


@pytest.mark.asyncio
async def test_approval_push_has_system_alert_and_agreement_target(monkeypatch):
    settings = SimpleNamespace(firebase_project_id='test', google_application_credentials=None,
        google_application_credentials_json='{}', firebase_service_account_json=None,
        frontend_public_url='https://example.test')
    monkeypatch.setattr(ns, 'get_settings', lambda: settings)
    monkeypatch.setattr(ns, '_ensure_firebase_app', lambda _: None)
    sent = []
    monkeypatch.setattr(ns.messaging, 'send', lambda message: sent.append(message) or 'test-message')
    session = session_with()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda:
        SimpleNamespace(all=lambda: [SimpleNamespace(token='test-device-token')])))
    service = ns.NotificationService(session)
    service._record_delivery = AsyncMock(return_value='provider_accepted')
    row = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(),
        type='partner_application_approved', title='تمت الموافقة على متجرك',
        message='راجع اتفاقية التاجر', body='راجع اتفاقية التاجر', extra_data={},
        payload={'deep_link': '/partner/agreement?required=1'}, aggregate_id=None)
    assert await service._send_mobile_push(row, str(uuid.uuid4())) == 'provider_accepted'
    message = sent[0]
    assert message.notification.title == row.title
    assert message.notification.body == row.body
    assert message.android.priority == 'high'
    assert message.android.notification.channel_id == 'luxury_notifications'
    assert message.data['deep_link'] == '/partner/agreement?required=1'
