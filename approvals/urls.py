from django.contrib.auth import views as auth_views
from django.urls import path, re_path

from . import views

urlpatterns = [
    path("login/", views.ThrottledLoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),

    # 媒体文件（现场照片/视频）需登录后访问
    re_path(r"^media/(?P<path>.*)$", views.protected_media, name="protected_media"),

    path("", views.home, name="home"),

    path("repair/", views.repair_list, name="repair_list"),
    path("repair/add/", views.repair_add, name="repair_add"),
    path("repair/<int:pk>/", views.repair_detail, name="repair_detail"),
    path("repair/<int:pk>/print/", views.repair_print, name="repair_print"),
    path("repair/<int:pk>/pdf/", views.repair_pdf, name="repair_pdf"),
    path("repair/<int:pk>/approve/", views.approve, name="approve"),
    path("repair/<int:pk>/resubmit/", views.resubmit, name="resubmit"),
    path("approval/", views.approval_queue, name="approval_queue"),
    path("notifications/", views.notification_list, name="notification_list"),

    # 设备档案（一物一码）
    path("equipment/", views.equipment_list, name="equipment_list"),
    path("equipment/add/", views.equipment_add, name="equipment_add"),
    path("equipment/<int:pk>/", views.equipment_detail, name="equipment_detail"),
    path("equipment/<int:pk>/edit/", views.equipment_edit, name="equipment_edit"),
    path("equipment/<int:pk>/qr/", views.equipment_qr_page, name="equipment_qr_page"),
    path("equipment/<int:pk>/qr.png", views.equipment_qr_image, name="equipment_qr_image"),

    path("dispatch/", views.dispatch_list, name="dispatch_list"),
    path("dispatch/create/", views.dispatch_create, name="dispatch_create"),
    path("dispatch/<int:pk>/", views.dispatch_detail, name="dispatch_detail"),
    path("dispatch/<int:pk>/accept/", views.dispatch_accept, name="dispatch_accept"),
    path("dispatch/<int:pk>/decline/", views.dispatch_reject, name="dispatch_reject"),
    path("dispatch/<int:pk>/reassign/", views.dispatch_reassign, name="dispatch_reassign"),
    path("dispatch/<int:pk>/rate/", views.dispatch_rate, name="dispatch_rate"),
    path("dispatch/<int:pk>/complete/", views.dispatch_complete, name="dispatch_complete"),
    path("dispatch/<int:pk>/review/", views.dispatch_review, name="dispatch_review"),
    path("dispatch/<int:pk>/final-review/", views.dispatch_final_review, name="dispatch_final_review"),
    path("dispatch/<int:pk>/settle/", views.settlement_create, name="settlement_create"),
    path("settlement/<int:pk>/confirm/", views.settlement_confirm, name="settlement_confirm"),

    # 备件库存
    path("spareparts/", views.sparepart_list, name="sparepart_list"),
    path("spareparts/add/", views.sparepart_add, name="sparepart_add"),
    path("spareparts/<int:pk>/", views.sparepart_detail, name="sparepart_detail"),
    path("spareparts/<int:pk>/edit/", views.sparepart_edit, name="sparepart_edit"),
    path("spareparts/<int:pk>/stock-in/", views.sparepart_stock_in, name="sparepart_stock_in"),
    path("spareparts/<int:pk>/stock-out/", views.sparepart_stock_out, name="sparepart_stock_out"),
]
