"""Django settings — Organt SNS (DRF + Vue SPA).

AI 기반 추천 서비스(SSAFY 13-PJT) 제출용. Organt(AI 직원들이 협업하는 회사)의 협업·소통을
네이티브로 보여주는 SNS. 비밀(API Key 등)은 .env에서 로드(NF1302).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# [NF1302 API Key 관리] 비밀은 .env(gitignore)에서만 — 저장소엔 .env.example만 둔다.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-0t*v^mut!q7k+5+&(4zc)_gva9rs=u3x$ffdjtnyj(zsv2m*^x",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") not in ("0", "false", "False")
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 3rd-party
    "rest_framework",
    "corsheaders",
    # local
    "sns",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# DB: 자체 서버에선 DATABASE_URL(Postgres)로 영속. 미설정 시 SQLITE_PATH(또는 기본 파일).
# Render 무료 플랜은 DATABASE_URL 없이 SQLite — 재배포마다 초기화(데모). 자체 서버는 영속.
_DB_URL = os.environ.get("DATABASE_URL", "").strip()
if _DB_URL:
    import dj_database_url
    DATABASES = {"default": dj_database_url.parse(_DB_URL, conn_max_age=600, ssl_require=False)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", "").strip() or (BASE_DIR / "db.sqlite3"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 빌드된 Vue SPA(dist) 경로 — 단일 출처 배포(F1305): Django가 /api 와 SPA를 함께 서빙.
SPA_DIST = os.environ.get("SPA_DIST") or str(BASE_DIR.parent / "frontend" / "dist")

# ── DRF (F1304 RESTful) ──────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 30,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_FILTER_BACKENDS": ["rest_framework.filters.OrderingFilter"],
}

# ── CORS (Vue SPA가 다른 포트에서 API 호출) ──────────────────────────
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o]

# ── Organt 두뇌 연동(ingest 소스) ────────────────────────────────────
ORGANT_PJT = os.environ.get("ORGANT_PJT", "/home/user/PJT")
ORGANT_LOGS = os.path.join(ORGANT_PJT, "logs")

# guide bridge 토큰(Phase 2) — 두뇌 러너만 출력 ingest 가능. 미설정이면 bridge 비활성(fail-closed).
ORGANT_GUIDE_TOKEN = os.environ.get("ORGANT_GUIDE_TOKEN", "")

# ── 생성형 AI (F1302 + 심화) — 키는 .env에서 ─────────────────────────
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "")   # GMS 등 OpenAI 호환 엔드포인트
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
