import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.database import SessionFactory
from backend.app.main import app
from backend.app.models.domain import User, UserRole, Profile, Order
from backend.app.security.passwords import hash_password


async def account(role='customer'):
    email=f'noura{uuid.uuid4().hex[:8]}@gmail.com'
    async with SessionFactory() as session:
        user=User(email=email,password_hash=hash_password('Admin1234'),is_active=True)
        session.add(user)
        await session.flush()
        session.add(Profile(id=user.id,user_id=user.id,email=email,full_name='نورة محمد'))
        session.add(UserRole(user_id=user.id,role=role))
        await session.commit()
        return user.id,email


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_order',[False,True])
async def test_app_review_accepts_registered_customer_without_delivery_and_keeps_moderation(pending_order):
    user_id,email=await account()
    _,admin_email=await account('admin')
    if pending_order:
        async with SessionFactory() as session:
            session.add(Order(user_id=user_id,order_number='ORD-'+uuid.uuid4().hex[:10],
                              status='pending',subtotal=7000,total=7100,shipping_total=100))
            await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://testserver') as client:
        login=await client.post('/auth/login',json={'email':email,'password':'Admin1234'})
        assert login.status_code==200,login.text
        headers={'Authorization':'Bearer '+login.json()['access_token']}
        eligibility=await client.get('/api/reviews/store/eligibility',headers=headers)
        assert eligibility.status_code==200
        assert eligibility.json()['data']['can_review'] is True
        assert eligibility.json()['data']['has_order']==pending_order
        body={'rating':5,'comment':'التطبيق واضح وسهل الاستخدام','show_name':False}
        assert (await client.post('/api/reviews/store',json=body)).status_code==401
        invalid=await client.post('/api/reviews/store',headers=headers,json={**body,'rating':6})
        assert invalid.status_code==422
        created=await client.post('/api/reviews/store',headers=headers,json=body)
        assert created.status_code==201,created.text
        review_id=created.json()['data']['id']
        public=(await client.get('/api/reviews/store/public')).json()['data']
        assert review_id not in [row['id'] for row in public]
        duplicate=await client.post('/api/reviews/store',headers=headers,json=body)
        assert duplicate.status_code==409,duplicate.text
        eligibility=await client.get('/api/reviews/store/eligibility',headers=headers)
        assert eligibility.json()['data']['reason']=='already_reviewed'
        assert eligibility.json()['data']['can_review'] is False
        admin_login=await client.post('/auth/login',json={'email':admin_email,'password':'Admin1234'})
        staff={'Authorization':'Bearer '+admin_login.json()['access_token']}
        approved=await client.patch('/api/reviews/store/'+review_id+'/status',headers=staff,json={'status':'approved'})
        assert approved.status_code==200,approved.text
        public=(await client.get('/api/reviews/store/public')).json()['data']
        saved=next(row for row in public if row['id']==review_id)
        assert saved.get('customer_name')!='نورة محمد'
        assert email not in json.dumps(public)
