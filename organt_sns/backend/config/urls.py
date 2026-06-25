"""URL 설정 — config.

/api/   : SNS REST API (DRF)
/admin/ : Django 관리자
그 외   : 빌드된 Vue SPA(dist) 서빙 — 단일 출처 배포(F1305). HTML5 history 모드라
          미지의 경로는 index.html로 폴백(클라이언트 라우팅). dist 미존재 시 안내.
"""
import os

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, HttpResponse
from django.urls import path, re_path, include
from django.views.static import serve as static_serve


def spa_index(request, *args, **kwargs):
    index = os.path.join(settings.SPA_DIST, "index.html")
    if os.path.exists(index):
        resp = FileResponse(open(index, "rb"))
        # index.html은 캐시 금지 — 배포로 JS 해시가 바뀌면 브라우저가 옛 해시를 물어
        # 404→빈 화면(blank)이 되던 문제. 해시된 assets/는 그대로 캐시돼도 안전.
        resp["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp["Pragma"] = "no-cache"
        return resp
    return HttpResponse(
        "<h1>Organt SNS</h1><p>프론트엔드 빌드가 없습니다. "
        "<code>cd frontend && npm install && npm run build</code> 후 새로고침하세요.</p>"
        "<p>API는 <a href='/api/stats/'>/api/</a> 에서 동작합니다.</p>",
        content_type="text/html; charset=utf-8",
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("sns.urls")),
    # 빌드 산출물(JS/CSS) 서빙
    re_path(r"^assets/(?P<path>.*)$", static_serve,
            {"document_root": os.path.join(settings.SPA_DIST, "assets")}),
    # SPA catch-all (api/admin/assets 제외) → index.html
    re_path(r"^(?!api/|admin/|assets/).*$", spa_index),
]
