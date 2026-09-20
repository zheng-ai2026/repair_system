from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    # /media/ 由 approvals.urls 中的 protected_media 处理（登录鉴权），不再公开直出
    path('', include('approvals.urls')),
]
