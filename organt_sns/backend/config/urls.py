"""URL 설정 — config. /api/ 아래 SNS REST API, /admin/ Django 관리자."""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("sns.urls")),
]
