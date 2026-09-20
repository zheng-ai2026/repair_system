"""
Django settings for repair_system project.

配置通过环境变量（或项目根目录 .env 文件）注入，禁止把真实密钥提交到仓库。
本地开发：复制 .env.example 为 .env 并按需修改；生产：由部署平台注入环境变量。
"""
import os
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    """极简 .env 加载（KEY=VALUE，# 注释），不引入额外依赖。不覆盖已有环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name, default):
    raw = os.environ.get(name, "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()] or default


# 生产环境必须通过环境变量注入 SECRET_KEY；未设置时直接失败，避免使用硬编码兜底密钥。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "未设置 DJANGO_SECRET_KEY。请在项目根目录 .env 中配置（可参考 .env.example），"
        "或通过环境变量注入。"
    )

# 默认安全（生产）；本地开发在 .env 中显式设置 DJANGO_DEBUG=True。
DEBUG = _env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'approvals',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'repair_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'approvals.context_processors.unread_notification_count',
            ],
        },
    },
]

WSGI_APPLICATION = 'repair_system.wsgi.application'


# Database —— 凭据全部来自环境变量，不入库
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("DB_ENGINE", "django.db.backends.postgresql"),
        "NAME": os.environ.get("DB_NAME", "repair_system_db"),
        "USER": os.environ.get("DB_USER", "postgres"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "localhost"),
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
}


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization

LANGUAGE_CODE = 'zh-hans'

TIME_ZONE = 'Asia/Shanghai'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# 上传内存占用上限：超过后落临时文件，避免大文件直接吃满内存
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024       # 5MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024       # 5MB

# 登录防爆破使用的本地内存缓存（单进程足够；多进程部署请换 Redis）
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "repair-system-default",
    }
}

# 结算单编号使用的安数部编码
SAFETY_DEPT_CODE = 'ASB'

# Default primary key field type

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ===================== 生产环境安全配置（DJANGO_SECURE=True 时生效）=====================
if _env_bool("DJANGO_SECURE", False):
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30          # 30 天，稳定后可调到一年
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    # 反向代理（nginx 等）终止 TLS 时按实际头协议判定 https
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # 代理下信任 X-Forwarded-Host，ALLOWED_HOSTS 仍做主机名校验
    USE_X_FORWARDED_HOST = True
