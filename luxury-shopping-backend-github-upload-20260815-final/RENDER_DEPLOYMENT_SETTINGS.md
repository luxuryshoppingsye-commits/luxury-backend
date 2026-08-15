# نشر Backend على Render مع Cloudflare R2

هذه النسخة مصممة بحيث صور المنتجات والمتاجر والصور العامة تُرفع إلى Cloudflare R2 مباشرة، ولا تُحفظ على قرص Render. لا تضف Persistent Disk لخدمة الـ Backend.

## إعداد الخدمة

- Service type: Web Service
- Runtime: Python
- Root directory: فارغ إذا كان هذا المستودع يحتوي الـ Backend وحده
- Build command: `python -m pip install -r requirements.txt`
- Pre-deploy command: `python -m alembic upgrade head`
- Start command: `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health/ready`
- Branch: `main`
- Persistent Disk: لا تضف Disk للصور

ملف `render.yaml` موجود داخل المشروع ويحتوي القيم العامة. المتغيرات التي قيمتها `sync: false` يجب إدخالها يدويًا في Render، ولا تضعها في GitHub.

## المتغيرات المطلوبة

### تشغيل وقاعدة البيانات

| المفتاح | القيمة |
| --- | --- |
| `APP_ENV` | `production` |
| `ALLOW_TEST_FIXTURES` | `false` |
| `PYTHON_VERSION` | `3.11.10` |
| `DATABASE_URL` | رابط Neon PostgreSQL التشغيلي |
| `DATABASE_MIGRATION_URL` | رابط قاعدة البيانات نفسه إذا كان مطلوبًا في إعدادك |
| `JWT_SECRET` | نفس قيمة الخدمة القديمة إذا أردت إبقاء الجلسات الحالية، أو قيمة عشوائية قوية جديدة |
| `RENDER_PUBLIC_URL` | `https://luxury-backend-xy9d.onrender.com` |
| `API_BASE_URL` | `https://luxury-backend-xy9d.onrender.com` |
| `APP_PUBLIC_URL` | `https://luxury-backend-xy9d.onrender.com` |
| `WS_BASE_URL` | `wss://luxury-backend-xy9d.onrender.com` |
| `FRONTEND_PUBLIC_URL` | `https://luxuryshoppings.com` |
| `CORS_ORIGINS` | `https://luxuryshoppings.com,https://www.luxuryshoppings.com` |
| `REALTIME_ALLOWED_ORIGINS` | `https://luxuryshoppings.com,https://www.luxuryshoppings.com` |

### Cloudflare R2 — إلزامي للإنتاج

| المفتاح | القيمة |
| --- | --- |
| `STORAGE_PROVIDER` | `r2` |
| `UPLOAD_DIR` | `/tmp/luxury-backend-upload-quarantine` |
| `R2_ENDPOINT_URL` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | `luxury-images-prod` |
| `R2_ACCESS_KEY_ID` | مفتاح R2 السري من Cloudflare |
| `R2_SECRET_ACCESS_KEY` | المفتاح السري من Cloudflare |
| `R2_REGION` | `auto` |
| `R2_PUBLIC_BASE_URL` | `https://images.luxuryshoppings.com` |
| `MAX_UPLOAD_BYTES` | `10485760` |

أنشئ في Cloudflare R2 bucket باسم `luxury-images-prod`، ثم اربط نطاق `images.luxuryshoppings.com` بالـ bucket. لا تستخدم رابط Render لعرض الصور.

### الخدمات الاختيارية المستخدمة في التطبيق

انقل القيم السرية من الخدمة القديمة يدويًا إلى الخدمة الجديدة، ولا ترسلها في المحادثة:

- `FIREBASE_PROJECT_ID`
- `FIREBASE_SERVICE_ACCOUNT_JSON` أو `GOOGLE_APPLICATION_CREDENTIALS_JSON`
- `GEMINI_API_KEY`
- `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`
- `EMAIL_PROVIDER=smtp`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`
- `RESEND_API_KEY`, `RESEND_FROM_EMAIL` لا تُستخدم مع `EMAIL_PROVIDER=smtp`؛ أضفها فقط إذا قررت التحويل إلى Resend لاحقًا

القيم العامة الخاصة بالذكاء الاصطناعي والإشعارات موجودة في `render.yaml`. المتغيرات السرية لا تُحفظ في GitHub.

## التحقق بعد النشر

افتح هذه المسارات:

1. `https://luxury-backend-xy9d.onrender.com/health/ready`
2. `https://luxury-backend-xy9d.onrender.com/health/live`
3. `https://luxury-backend-xy9d.onrender.com/health/storage`

يجب أن يظهر في فحص التخزين `provider=cloudflare_r2` و`reachable=true`.

بعد رفع صورة اختبارية، يجب أن يبدأ رابطها بـ `https://images.luxuryshoppings.com/`. إذا ظهر رابط `/uploads/` أو رابط Render للصورة فالإعداد غير صحيح ويجب إيقاف النشر قبل استخدامه.
