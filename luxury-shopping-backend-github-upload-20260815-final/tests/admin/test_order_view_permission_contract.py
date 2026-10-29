from __future__ import annotations

import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models.domain import Order, OrderItem, Profile, User, UserRole
from backend.app.security.passwords import hash_password


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['customer', 'employee', 'staff', 'finance', 'logistics'])
async def test_order_view_grant_is_honored_and_revoked_without_write_access(role: str) -> None:
    settings = get_settings()
    assert settings.is_test_environment and settings.allow_test_fixtures
    assert urlsplit(settings.database_url).hostname == '127.0.0.1'
    suffix = uuid.uuid4().hex[:8]
    users = []
    async with SessionFactory() as session:
        for name, assigned_role in [('sara', 'admin'), ('nasser', role), ('ghaya', 'customer')]:
            user = User(email=f'{name}-{suffix}@gmail.com', password_hash=hash_password('Admin1234'), is_active=True)
            session.add(user)
            await session.flush()
            session.add(Profile(id=user.id, user_id=user.id, email=user.email, full_name=name, phone='777123456'))
            session.add(UserRole(user_id=user.id, role=assigned_role))
            users.append(user)
        order = Order(order_number=f'INV-{suffix.upper()}', user_id=users[2].id,
                      created_by=users[2].id, status='pending', total=Decimal('7000.00'),
                      subtotal=Decimal('7000.00'), discount_total=0, shipping_total=0,
                      currency_code='YER', payment_method='wallet_transfer', payment_status='pending')
        session.add(order)
        await session.flush()
        session.add(OrderItem(order_id=order.id, product_name='فستان كتان', quantity=1,
                              unit_price=Decimal('7000.00'), total_price=Decimal('7000.00')))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        headers = []
        for user in users:
            login = await client.post('/auth/login', json={'email': user.email, 'password': 'Admin1234'})
            assert login.status_code == 200, login.text
            headers.append({'Authorization': f"Bearer {login.json()['access_token']}"})
        permissions_url = f'/api/admin/staff/permissions/{users[1].id}'
        granted = await client.put(permissions_url, headers=headers[0], json={'permissions': ['orders.view']})
        assert granted.status_code == 200, granted.text
        for path in ['/orders', '/api/orders']:
            listed = await client.get(path, headers=headers[1], params={'scope': 'admin'})
            assert listed.status_code == 200, listed.text
            rows = listed.json() if path == '/orders' else listed.json()['data']
            assert str(order.id) in {row['id'] for row in rows}
        personal = await client.get('/orders', headers=headers[1])
        assert personal.status_code == 200
        assert str(order.id) not in {row['id'] for row in personal.json()}
        for path in [f'/orders/{order.id}', f'/api/orders/{order.id}']:
            detail = await client.get(path, headers=headers[1])
            assert detail.status_code == 200, detail.text
            assert detail.json()['items'][0]['product_name'] == 'فستان كتان'
        for path in ['/api/admin/profiles/lookup', '/api/operations/api/admin/profiles/lookup']:
            profiles = await client.post(path, headers=headers[1], json={'user_ids': [str(users[2].id)]})
            assert profiles.status_code == 200, profiles.text
            assert profiles.json()['data'][0]['full_name'] == 'ghaya'
            assert not {'extra_data', 'email', 'classification', 'password_hash'}.intersection(profiles.json()['data'][0])
        payments = await client.get(f'/api/payments/orders/{order.id}', headers=headers[1])
        assert payments.status_code == 200, payments.text
        for path in [f'/orders/{order.id}/courier-assignment', f'/api/admin/orders/{order.id}/courier-assignment']:
            assignment = await client.get(path, headers=headers[1])
            assert assignment.status_code == 200, assignment.text
        mutation = await client.patch(f'/api/orders/{order.id}/status', headers=headers[1], json={'status': 'processing'})
        assert mutation.status_code == 403, mutation.text
        for operation, body in [('assignee', {'user_id': None}), ('courier-assignment', {'courier_id': None})]:
            denied_write = await client.patch(f'/api/admin/orders/{order.id}/{operation}', headers=headers[1], json=body)
            assert denied_write.status_code == 403, denied_write.text
        async with SessionFactory() as session:
            assert (await session.execute(select(Order.status).where(Order.id == order.id))).scalar_one() == 'pending'
        revoked = await client.put(permissions_url, headers=headers[0], json={'permissions': []})
        assert revoked.status_code == 200
        for path in ['/orders?scope=admin', f'/orders/{order.id}', '/api/admin/profiles/lookup', f'/api/payments/orders/{order.id}']:
            response = await client.post(path, headers=headers[1], json={'user_ids': [str(users[2].id)]}) if path.endswith('/lookup') else await client.get(path, headers=headers[1])
            assert response.status_code in (403, 404), (path, response.text)
        owner = await client.get(f'/orders/{order.id}', headers=headers[2])
        assert owner.status_code == 200, owner.text
        stranger = await client.get('/orders?scope=admin', headers=headers[2])
        assert stranger.status_code == 403, stranger.text
