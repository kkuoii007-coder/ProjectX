import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env", override=False)


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    return int(env(name, default))


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = False
ALLOWED_HOSTS = [h.strip() for h in env("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "apps.accounts",
    "apps.credits",
    "apps.library",
    "apps.sources",
    "apps.workshop",
    "apps.ai",
    "apps.operations",
]

MIDDLEWARE = [
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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.account_summary",
            ],
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "philosophy_reader"),
        "USER": env("DB_USER", "philosophy_reader"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 5},
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.LoginBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "account:dashboard"
LOGOUT_REDIRECT_URL = "library:home"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = BASE_DIR / "media" / "private"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

# Central product defaults from docs/spec/docs/configuration.md.
UI_LANGUAGE = "ru"
TEXT_LANGUAGE = "en"
TRANSLATION_LANGUAGE = "ru"
WELCOME_CREDITS = env_int("WELCOME_CREDITS", 30)
WELCOME_CAMPAIGN = "welcome-v1"
CREDIT_COSTS = {
    "generate": env_int("COST_GENERATE_SERIES", 10),
    "edit_series": env_int("COST_EDIT_SERIES", 10),
    "edit_part": env_int("COST_EDIT_PART", 3),
    "edit_sentence": env_int("COST_EDIT_SENTENCE", 1),
    "ask": env_int("COST_ASK", 1),
}
CREDIT_POLICY_VERSION = "credits-v1"
TOPUP_MODE = env("TOPUP_MODE", "manual_test")
REGISTRATION_MODE = env("REGISTRATION_MODE", "invite")
USER_ACTIVE_AI_JOBS = 1
AI_WORKER_CONCURRENCY = 1
AI_LEASE_SECONDS = 300
AI_UNRESOLVED_USER_TIMEOUT_MINUTES = 15
GLOBAL_QUEUE_CAPACITY = 50
MAX_USER_PROMPT_CHARS = 2000
MAX_EDIT_INSTRUCTION_CHARS = 1000
USER_AI_REQUESTS_PER_HOUR = 20
WEB_SEARCH_ENABLED = False
WEB_FETCH_ENABLED = False
COMMUNITY_ENABLED = False

# Stage 5: sources and RAG (docs/spec/docs/configuration.md).
SOURCE_UPLOAD_MAX_BYTES = env_int("SOURCE_UPLOAD_MAX_BYTES", 5 * 1024 * 1024)
SOURCE_MAX_PAGES = env_int("SOURCE_MAX_PAGES", 150)
SOURCE_CHUNK_WORDS_MIN = env_int("SOURCE_CHUNK_WORDS_MIN", 300)
SOURCE_CHUNK_WORDS_MAX = env_int("SOURCE_CHUNK_WORDS_MAX", 600)
SOURCE_FETCH_MAX_BYTES = env_int("SOURCE_FETCH_MAX_BYTES", 5 * 1024 * 1024)
SOURCE_FETCH_TIMEOUT_SECONDS = env_int("SOURCE_FETCH_TIMEOUT_SECONDS", 20)
SOURCE_FETCH_MAX_REDIRECTS = env_int("SOURCE_FETCH_MAX_REDIRECTS", 3)
SOURCE_DOMAIN_ALLOWLIST = [d.strip().lower() for d in env("SOURCE_DOMAIN_ALLOWLIST", "").split(",") if d.strip()]
SEARCH_PROVIDER = env("SEARCH_PROVIDER", "")
SEARCH_RESULTS_MAX = env_int("SEARCH_RESULTS_MAX", 5)
USER_ACTIVE_DOCUMENTS_MAX = env_int("USER_ACTIVE_DOCUMENTS_MAX", 10)
RETRIEVAL_MAX_CHUNKS = env_int("RETRIEVAL_MAX_CHUNKS", 8)
EMBEDDING_BATCH_SIZE = env_int("EMBEDDING_BATCH_SIZE", 16)

LIVE_AI_ENABLED = env_bool("LIVE_AI_ENABLED", False)
AI_PROVIDER = env("AI_PROVIDER", "fake")
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", "")
AI_GENERATION_MODEL = env("AI_GENERATION_MODEL", "anthropic/claude-haiku-5.5")
AI_REVIEW_MODEL = env("AI_REVIEW_MODEL", "anthropic/claude-sonnet-5.5")
AI_EMBEDDING_MODEL = env("AI_EMBEDDING_MODEL", "openai/text-embedding-3-small")
AI_EMBEDDING_DIMENSIONS = env_int("AI_EMBEDDING_DIMENSIONS", 1536)
AI_HTTP_TIMEOUT_SECONDS = 120
AI_MAX_PROVIDER_CALLS_PER_JOB = 4
OWNER_DAILY_BUDGET_USD = env("OWNER_DAILY_BUDGET_USD")
OWNER_MONTHLY_BUDGET_USD = env("OWNER_MONTHLY_BUDGET_USD")
AI_MAX_INPUT_TOKENS = env_int("AI_MAX_INPUT_TOKENS", 60000)
AI_MAX_OUTPUT_TOKENS = env_int("AI_MAX_OUTPUT_TOKENS", 8000)
# Price ceilings per stage in USD per 1M tokens (in, out) — refuse a model
# whose listed price exceeds the ceiling instead of silently overspending.
AI_STAGE_PRICE_CEILINGS = {"generation": (3.0, 15.0), "review": (3.0, 15.0)}
# Providers allowed for generation (privacy policy check); empty = any OpenRouter route.
AI_PROVIDER_ALLOWLIST = [p.strip() for p in env("AI_PROVIDER_ALLOWLIST", "anthropic").split(",") if p.strip()]
AI_PRIVACY_POLICY = env("AI_PRIVACY_POLICY", "no-training")
