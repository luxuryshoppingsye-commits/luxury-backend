from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models import MODEL_BY_TABLE
from backend.app.models.domain import Profile, User, UserRole
from backend.app.security.passwords import hash_password
from backend.app.services import notification_service as ns


async def accounts():
    settings = get_settings()
    assert settings.is_test_environment and settings.allow_test_fixtures
    assert urlsplit(settings.database_url).hostname == '127.0.0.1'
    suffix = uuid.uuid4().hex[:8]
    users = []
    async with SessionFactory() as session:
        for name, role in [('sara', 'admin'), ('ghaya', 'customer'), ('reem', 'customer')]:
            user = User(email=f'{name}-{suffix}@gmail.com', password_hash=hash_password('Admin1234'), is_active=True)
            session.add(user)
            await session.flush()
            session.add(Profile(id=user.id, user_id=user.id, email=user.email, full_name=name))
            session.add(UserRole(user_id=user.id, role=role))
            users.append(user)
        await session.commit()
    return users


async def sign_in(client, users):
    headers = []
    for user in users:
        response = await client.post('/auth/login', json={'email': user.email, 'password': 'Admin1234'})
        assert response.status_code == 200, response.text
        headers.append({'Authorization': f"Bearer {response.json()['access_token']}"})
    return headers


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['announcement', 'update', 'alert'])
async def test_admin_app_notification_is_visible_private_and_never_queues_email(kind, monkeypatch):
    users = await accounts()
    monkeypatch.setattr(ns, 'email_delivery_configured', lambda _: True)
    monkeypatch.setattr(ns, '_ensure_firebase_app', lambda _: None)
    monkeypatch.setattr(get_settings(), 'google_application_credentials_json', '{}')
    sent = []
    monkeypatch.setattr(ns.messaging, 'send', lambda message: sent.append(message) or 'accepted-message')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        headers = await sign_in(client, users)
        created = await client.post('/api/notifications/admin/broadcast', headers=headers[0], json={
            'title': 'وصلت فساتين جديدة', 'message': 'تشكيلة فساتين كتان جديدة متاحة الآن.',
            'type': kind, 'userIds': [str(users[1].id)],
        })
        assert created.status_code == 201, created.text
        listed = await client.get('/api/notifications', headers=headers[1])
        assert listed.status_code == 200, listed.text
        assert listed.json()['total'] == 1, listed.text
        assert listed.json()['unread'] == 1, listed.text
        other = await client.get('/api/notifications', headers=headers[2])
        assert other.json()['total'] == 0
        registered = await client.post('/notifications/device-token', headers=headers[1], json={
            'token': 'ghaya-mobile-'+uuid.uuid4().hex, 'deviceId': 'ghaya-phone', 'platform': 'android',
        })
        assert registered.status_code == 200, registered.text
        async with SessionFactory() as session:
            outbox = MODEL_BY_TABLE['notification_outbox']
            row = (await session.execute(select(outbox).where(outbox.user_id == users[1].id))).scalar_one()
            assert row.extra_data['delivery_channels'] == ['in_app', 'mobile_push', 'web_push']
            await ns.NotificationService(session).process_outbox_once(limit=500)
            await session.commit()
            assert row.extra_data['delivered_channels']['mobile_push'] == 'provider_accepted'
            compatible_type = {'announcement': 'admin_broadcast_message', 'update': 'info', 'alert': 'general_alert'}[kind]
            assert any(message.data['notification_type'] == compatible_type and message.data['type'] == kind for message in sent)
            emails = (await session.execute(select(MODEL_BY_TABLE['email_outbox']).where(
                MODEL_BY_TABLE['email_outbox'].user_id == users[1].id))).scalars().all()
            assert emails == []
        repeated = await client.post('/api/notifications/admin/broadcast', headers=headers[0], json={
            'title': 'وصلت فساتين جديدة', 'message': 'تشكيلة فساتين كتان جديدة متاحة الآن.',
            'type': kind, 'userIds': [str(users[1].id)],
        })
        assert repeated.status_code == 201
        listed_again = await client.get('/api/notifications', headers=headers[1])
        assert listed_again.json()['total'] == 2


@pytest.mark.asyncio
async def test_new_device_resumes_recent_unexpired_failed_push_without_duplicates(monkeypatch):
    users = await accounts()
    outbox = MODEL_BY_TABLE['notification_outbox']
    now = datetime.now(timezone.utc)
    async with SessionFactory() as session:
        service = ns.NotificationService(session)
        rows = []
        for name, target, age, expiry, delivered in [
            ('recent', users[1].id, 0, None, {'in_app': 'sent', 'email': 'sent'}),
            ('other', users[2].id, 0, None, {}),
            ('old', users[1].id, 48, None, {}),
            ('expired', users[1].id, 0, now-timedelta(minutes=1), {}),
            ('delivered', users[1].id, 0, None, {'mobile_push': 'provider_accepted'}),
        ]:
            notification = await service.create_notification(ns.NotificationPayload(
                user_id=target, title='فساتين كتان', body='تشكيلة فساتين جديدة متاحة في المتجر.',
                notification_type='promotion', expires_at=expiry,
                delivery_channels=('in_app', 'mobile_push'), deduplication_key=uuid.uuid4().hex,
            ))
            row = (await session.execute(select(outbox).where(outbox.payload['notification_id'].astext == str(notification.id)))).scalar_one()
            row.status = 'dead_letter'
            row.attempts = 5
            row.created_at = now-timedelta(hours=age)
            row.last_error = 'mobile_push:failed_retryable'
            row.extra_data = {**row.extra_data, 'delivered_channels': delivered}
            rows.append(row)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        headers = await sign_in(client, users)
        registration = {'token': 'ghaya-mobile-'+uuid.uuid4().hex, 'deviceId': 'ghaya-phone', 'platform': 'android'}
        response = await client.post('/notifications/device-token', headers=headers[1], json=registration)
        assert response.status_code == 200, response.text
        async with SessionFactory() as session:
            recent = await session.get(outbox, rows[0].id)
            assert recent.status == 'queued'
            assert recent.attempts == 0
            assert recent.extra_data['delivered_channels'] == {'in_app': 'sent', 'email': 'sent'}
            for row in rows[1:]:
                assert (await session.get(outbox, row.id)).status == 'dead_letter'
            recent.status = 'dead_letter'
            recent.last_error = 'mobile_push:failed_retryable'
            await session.commit()
        await client.post('/notifications/device-token', headers=headers[1], json=registration)
        async with SessionFactory() as session:
            assert (await session.get(outbox, rows[0].id)).status == 'dead_letter'
        registration['token'] = 'ghaya-refreshed-'+uuid.uuid4().hex
        await client.post('/notifications/device-token', headers=headers[1], json=registration)
        async with SessionFactory() as session:
            recent = await session.get(outbox, rows[0].id)
            assert recent.status == 'queued'
            service = ns.NotificationService(session)
            from unittest.mock import AsyncMock
            service._deliver_channel = AsyncMock(return_value='provider_accepted')
            assert (await service._deliver_outbox(recent))['ok']
            service._deliver_channel.assert_awaited_once_with(recent, str(recent.payload['notification_id']), 'mobile_push')
            assert (await service._deliver_outbox(recent))['ok']
            assert service._deliver_channel.await_count == 1


@pytest.mark.asyncio
async def test_explicit_broadcast_retry_key_is_scoped_to_each_recipient():
    users = await accounts()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        headers = await sign_in(client, users)
        body = {'title': 'تحديث مواعيد التوصيل', 'message': 'تابع موعد التوصيل في تفاصيل طلبك.',
                'type': 'update', 'userIds': [str(user.id) for user in users[1:]],
                'deduplicationKey': uuid.uuid4().hex}
        for _ in range(2):
            response = await client.post('/api/notifications/admin/broadcast', headers=headers[0], json=body)
            assert response.status_code == 201
        for owner in headers[1:]:
            listed = await client.get('/api/notifications', headers=owner)
            assert listed.json()['total'] == 1, listed.text
