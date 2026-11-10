
"""Environment-backed settings for the Flask application."""

import os
from datetime import timedelta


class Config:
    """Application settings."""

    ENVIRONMENT = os.environ.get("APP_ENV", "development")
    DEBUG = os.environ.get("APP_DEBUG", "0").strip().lower() in {
        "1", "true", "yes"
    }
    DATABASE_URL = os.environ.get("DATABASE_URL")
    SECRET_KEY = os.environ.get("SECRET_KEY")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = ENVIRONMENT == "production"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    SESSION_REFRESH_EACH_REQUEST = False

    TESTING = False
