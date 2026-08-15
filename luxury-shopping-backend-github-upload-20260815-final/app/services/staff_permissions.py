from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.domain import StaffPermissionSet


def _group(key: str, label: str, *permissions: tuple[str, str]) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "permissions": tuple({"key": item_key, "label": item_label} for item_key, item_label in permissions),
    }


# This is the single permission vocabulary used by the web, Windows dashboard,
# and server. It covers every administration module shown in the supplied
# product screenshots, not only the original coarse 28 permissions.
PERMISSION_GROUPS: tuple[dict[str, Any], ...] = (
    _group("dashboard", "لوحة القيادة", ("dashboard.view", "عرض لوحة القيادة"), ("operations.view", "عرض التحكم التشغيلي")),
    _group(
        "orders_customers",
        "الطلبات والعملاء",
        ("orders.view", "عرض الطلبات"),
        ("orders.create", "إنشاء طلب يدوي"),
        ("orders.update", "تحديث حالة الطلب"),
        ("orders.delete", "حذف الطلبات"),
        ("orders.export", "تصدير الطلبات"),
        ("local_requests.view", "عرض التسوق المحلي"),
        ("local_requests.create", "إنشاء طلب تسوق محلي"),
        ("local_requests.update", "تحديث طلب التسوق المحلي"),
        ("local_requests.delete", "حذف طلب التسوق المحلي"),
        ("local_requests.assign", "تعيين مسؤول للطلب المحلي"),
        ("customers.view", "عرض العملاء"),
        ("customers.create", "إضافة عميل"),
        ("customers.update", "تعديل بيانات العملاء"),
        ("customers.delete", "حذف العملاء"),
        ("customers.export", "تصدير العملاء"),
        ("messages.view", "عرض رسائل العملاء"),
        ("messages.send", "إرسال رسائل للعملاء"),
        ("messages.delete", "حذف الرسائل"),
        ("notifications.view", "عرض الإشعارات"),
        ("notifications.send", "إرسال إشعارات"),
        ("notifications.delete", "حذف الإشعارات"),
        ("reviews.view", "عرض تقييمات المتجر"),
        ("reviews.update", "إدارة تقييمات المتجر"),
        ("reviews.delete", "حذف تقييمات المتجر"),
    ),
    _group(
        "products_inventory",
        "المنتجات والمخزون",
        ("products.view", "عرض المنتجات"),
        ("products.create", "إضافة منتج"),
        ("products.update", "تعديل منتج"),
        ("products.delete", "حذف منتج"),
        ("products.activate", "تفعيل وتعطيل المنتجات"),
        ("products.feature", "تمييز المنتجات"),
        ("products.import", "استيراد المنتجات"),
        ("product_approval.view", "عرض موافقات المنتجات"),
        ("product_approval.approve", "اعتماد المنتجات"),
        ("product_approval.reject", "رفض المنتجات"),
        ("warehouses.view", "عرض المستودعات"),
        ("warehouses.create", "إضافة مستودع"),
        ("warehouses.update", "تعديل مستودع"),
        ("warehouses.delete", "حذف مستودع"),
        ("inventory.view", "عرض المخزون"),
        ("inventory.create", "إضافة حركة مخزون"),
        ("inventory.update", "تعديل المخزون"),
        ("inventory.adjust", "تسوية المخزون"),
        ("inventory.delete", "حذف حركة مخزون"),
        ("brands.view", "عرض الماركات"),
        ("brands.create", "إضافة ماركة"),
        ("brands.update", "تعديل ماركة"),
        ("brands.delete", "حذف ماركة"),
        ("product_options.view", "عرض خيارات المنتجات"),
        ("product_options.create", "إضافة خيار منتج"),
        ("product_options.update", "تعديل خيار منتج"),
        ("product_options.delete", "حذف خيار منتج"),
        ("categories.view", "عرض الأقسام"),
        ("categories.create", "إضافة قسم"),
        ("categories.update", "تعديل قسم"),
        ("categories.delete", "حذف قسم"),
        ("suppliers.view", "عرض الموردين والتجار"),
        ("suppliers.create", "إضافة مورد أو تاجر"),
        ("suppliers.update", "تعديل مورد أو تاجر"),
        ("suppliers.delete", "حذف مورد أو تاجر"),
        ("couriers.view", "عرض مندوبي التوصيل"),
        ("couriers.create", "إضافة مندوب توصيل"),
        ("couriers.update", "تعديل مندوب توصيل"),
        ("couriers.delete", "حذف مندوب توصيل"),
        ("couriers.assign", "تعيين مندوب للتوصيل"),
    ),
    _group(
        "finance_accounting",
        "المالية والمحاسبة",
        ("accounting.view", "عرض المركز المحاسبي"),
        ("accounting.update", "إدارة المركز المحاسبي"),
        ("finance.view", "عرض المالية"),
        ("finance.update", "إدارة المدفوعات والتسويات"),
        ("reports.view", "عرض التقارير المالية"),
        ("reports.export", "تصدير التقارير المالية"),
        ("vouchers.view", "عرض السندات المالية"),
        ("vouchers.create", "إضافة سند مالي"),
        ("vouchers.update", "تعديل سند مالي"),
        ("vouchers.delete", "حذف سند مالي"),
        ("currencies.view", "عرض العملات"),
        ("currencies.update", "إدارة العملات"),
    ),
    _group(
        "partners_marketing",
        "التجار والمسوقون",
        ("partners.view", "عرض التجار"),
        ("partners.create", "إضافة تاجر"),
        ("partners.update", "تعديل تاجر"),
        ("partners.delete", "حذف تاجر"),
        ("partner_applications.view", "عرض طلبات التجار"),
        ("partner_applications.approve", "اعتماد طلبات التجار"),
        ("partner_applications.reject", "رفض طلبات التجار"),
        ("local_merchants.view", "عرض المتاجر المحلية"),
        ("local_merchants.create", "إضافة متجر محلي"),
        ("local_merchants.update", "تعديل متجر محلي"),
        ("local_merchants.delete", "حذف متجر محلي"),
        ("marketers.view", "عرض المسوقين"),
        ("marketers.create", "إضافة مسوق"),
        ("marketers.update", "تعديل مسوق"),
        ("marketers.delete", "حذف مسوق"),
        ("marketing.view", "عرض مركز التسويق"),
        ("marketing.create", "إنشاء حملة تسويقية"),
        ("marketing.update", "تعديل حملة تسويقية"),
        ("marketing.delete", "حذف حملة تسويقية"),
        ("marketing.send", "إرسال حملة تسويقية"),
        ("coupons.view", "عرض الكوبونات"),
        ("coupons.create", "إضافة كوبون"),
        ("coupons.update", "تعديل كوبون"),
        ("coupons.delete", "حذف كوبون"),
        ("loyalty.view", "عرض برنامج الولاء"),
        ("loyalty.create", "إضافة قاعدة ولاء"),
        ("loyalty.update", "تعديل برنامج الولاء"),
        ("loyalty.delete", "حذف قاعدة ولاء"),
    ),
    _group(
        "international",
        "التسوق الدولي",
        ("international.view", "عرض طلبات التسوق الدولي"),
        ("international.create", "إنشاء طلب تسوق دولي"),
        ("international.update", "تحديث طلب التسوق الدولي"),
        ("international.delete", "حذف طلب التسوق الدولي"),
        ("purchases.view", "عرض المشتريات الدولية"),
        ("purchases.create", "إضافة عملية شراء دولية"),
        ("purchases.update", "تعديل عملية شراء دولية"),
        ("purchases.delete", "حذف عملية شراء دولية"),
        ("order_linking.view", "عرض ربط الطلبات"),
        ("order_linking.update", "إدارة ربط الطلبات"),
        ("global_sites.view", "عرض المواقع العالمية"),
        ("global_sites.create", "إضافة موقع عالمي"),
        ("global_sites.update", "تعديل موقع عالمي"),
        ("global_sites.delete", "حذف موقع عالمي"),
    ),
    _group(
        "content_design",
        "المحتوى والتصميم",
        ("blog.view", "عرض المدونة"),
        ("blog.create", "إضافة مقال"),
        ("blog.update", "تعديل مقال"),
        ("blog.delete", "حذف مقال"),
        ("blog.publish", "نشر مقال"),
        ("design.view", "عرض لوحة التصميم"),
        ("design.update", "تعديل التصميم"),
        ("theme.view", "عرض الثيم والألوان"),
        ("theme.update", "تعديل الثيم والألوان"),
        ("site_settings.view", "عرض إعدادات الموقع"),
        ("site_settings.update", "تعديل إعدادات الموقع"),
        ("banners.view", "عرض البنرات"),
        ("banners.create", "إضافة بنر"),
        ("banners.update", "تعديل بنر"),
        ("banners.delete", "حذف بنر"),
        ("pages.view", "عرض الصفحات الثابتة"),
        ("pages.create", "إضافة صفحة ثابتة"),
        ("pages.update", "تعديل صفحة ثابتة"),
        ("pages.delete", "حذف صفحة ثابتة"),
        ("pages.publish", "نشر صفحة ثابتة"),
        ("elements.view", "عرض العناصر المخصصة"),
        ("elements.create", "إضافة عنصر مخصص"),
        ("elements.update", "تعديل عنصر مخصص"),
        ("elements.delete", "حذف عنصر مخصص"),
        ("forms.view", "عرض النماذج"),
        ("forms.create", "إضافة نموذج"),
        ("forms.update", "تعديل نموذج"),
        ("forms.delete", "حذف نموذج"),
        ("content.view", "عرض محتوى الموقع"),
        ("content.create", "إضافة محتوى"),
        ("content.update", "تعديل محتوى الموقع"),
        ("content.delete", "حذف محتوى الموقع"),
        ("content.publish", "نشر محتوى الموقع"),
    ),
    _group(
        "reports_audit",
        "التقارير والمراقبة",
        ("kpis.view", "عرض مؤشرات الأداء"),
        ("kpis.export", "تصدير مؤشرات الأداء"),
        ("analytics.view", "عرض تحليلات السلوك"),
        ("analytics.export", "تصدير تحليلات السلوك"),
        ("forecasting.view", "عرض توقعات المبيعات"),
        ("forecasting.generate", "توليد توقعات المبيعات"),
        ("activity_log.view", "عرض سجل الأحداث"),
        ("activity_log.export", "تصدير سجل الأحداث"),
        ("access_logs.view", "عرض سجل الوصول"),
        ("access_logs.export", "تصدير سجل الوصول"),
        ("tickets.view", "عرض تذاكر الدعم"),
        ("tickets.create", "إنشاء تذكرة دعم"),
        ("tickets.update", "تحديث تذكرة دعم"),
        ("tickets.close", "إغلاق تذكرة دعم"),
        ("shipping.view", "عرض إدارة الشحن"),
        ("shipping.update", "تحديث إدارة الشحن"),
        ("shipping.assign", "تعيين الشحن والتوصيل"),
        ("security_audit.view", "عرض التدقيق الأمني"),
        ("security_audit.export", "تصدير التدقيق الأمني"),
    ),
    _group(
        "staff",
        "الموظفون والصلاحيات",
        ("staff.view", "عرض الموظفين"),
        ("staff.create", "إضافة موظف"),
        ("staff.update", "تعديل أدوار وصلاحيات الموظفين"),
        ("staff.delete", "حذف موظف"),
        ("staff.assign", "تعيين موظف لعملية"),
    ),
    _group(
        "settings_backup",
        "الإعدادات والنسخ الاحتياطي",
        ("settings.view", "عرض الإعدادات العامة"),
        ("settings.update", "تعديل الإعدادات العامة"),
        ("data.view", "عرض الاستيراد والتصدير"),
        ("data.import", "استيراد البيانات"),
        ("data.export", "تصدير البيانات"),
        ("backup.view", "عرض النسخ الاحتياطي"),
        ("backup.create", "إنشاء نسخة احتياطية"),
        ("backup.restore", "استعادة نسخة احتياطية"),
        ("backup.delete", "حذف نسخة احتياطية"),
    ),
)

ALL_PERMISSIONS: frozenset[str] = frozenset(
    item["key"]
    for group in PERMISSION_GROUPS
    for item in group["permissions"]
)

# Defaults preserve the existing role behavior until an administrator saves a
# custom allow-list for a particular employee.
ROLE_DEFAULTS: dict[str, frozenset[str]] = {
    "admin": ALL_PERMISSIONS,
    "manager": ALL_PERMISSIONS,
    "finance": frozenset({
        "dashboard.view", "orders.view", "customers.view", "customers.update",
        "finance.view", "finance.update", "products.view", "inventory.view",
    }),
    "logistics": frozenset({
        "dashboard.view", "orders.view", "orders.update", "customers.view",
        "products.view", "products.update", "products.activate", "products.feature",
        "inventory.view", "inventory.update", "shipping.view", "shipping.update",
    }),
    "staff": frozenset({
        "dashboard.view", "orders.view", "orders.create", "orders.update",
        "customers.view", "customers.update", "products.view", "products.create",
        "products.update", "products.activate", "products.feature", "inventory.view",
    }),
    "employee": frozenset({
        "dashboard.view", "orders.view", "orders.create", "orders.update",
        "customers.view", "customers.update", "products.view", "products.create",
        "products.update", "products.activate", "products.feature", "inventory.view",
    }),
    "partner": frozenset({
        "products.view", "products.create", "products.update", "products.delete",
        "products.activate", "products.feature", "dashboard.view",
    }),
}


def normalize_permissions(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip() in ALL_PERMISSIONS})


def default_permissions_for_roles(roles: set[str]) -> set[str]:
    if "admin" in roles:
        return set(ALL_PERMISSIONS)
    result: set[str] = set()
    for role in roles:
        result.update(ROLE_DEFAULTS.get(role, ()))
    return result


async def explicit_permissions_for(session: AsyncSession, user_id: UUID) -> list[str] | None:
    try:
        row = await session.get(StaffPermissionSet, user_id)
    except SQLAlchemyError:
        # A rolling deployment may start the API before the migration is
        # applied. Fall back safely to role defaults instead of breaking login.
        await session.rollback()
        return None
    return normalize_permissions(row.permissions) if row is not None else None


async def effective_permissions(session: AsyncSession, user_id: UUID, roles: set[str]) -> set[str]:
    if "admin" in roles:
        return set(ALL_PERMISSIONS)
    explicit = await explicit_permissions_for(session, user_id)
    return set(explicit) if explicit is not None else default_permissions_for_roles(roles)


async def require_staff_permission(
    session: AsyncSession,
    user_id: UUID,
    roles: set[str],
    permission: str,
) -> None:
    if permission not in ALL_PERMISSIONS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="permission_denied")
    if permission not in await effective_permissions(session, user_id, roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "staff_permission_denied", "permission": permission},
        )


def capabilities_for_permissions(permissions: set[str]) -> dict[str, bool]:
    return {permission: permission in permissions for permission in ALL_PERMISSIONS}
