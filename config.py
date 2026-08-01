"""
AtiFixia SBIR Intelligence Platform — Configuration
Reads from environment variables / .env file.
"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # AI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    ANTHROPIC_API_KEY: str = ""

    # Database — Neon PostgreSQL (set in .env / Heroku config vars)
    DATABASE_URL: str = "sqlite+aiosqlite:///./sbir_platform.db"   # fallback for local-only dev
    SYNC_DATABASE_URL: str = ""

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""
    SUPABASE_BUCKET: str = "sbir-documents"

    # Auth
    SECRET_KEY: str = "CHANGE_ME_IN_PRODUCTION"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Square Payments
    SQUARE_ENVIRONMENT: str = "sandbox"           # "sandbox" or "production"
    SQUARE_PRODUCTION_APPLICATION_ID: str = ""
    SQUARE_PRODUCTION_ACCESS_TOKEN: str = ""
    SQUARE_SANDBOX_APPLICATION_ID: str = ""
    SQUARE_SANDBOX_ACCESS_TOKEN: str = ""
    SQUARE_APP_NAME: str = "AtiFixia"
    # Optional: override the auto-detected Square location ID
    SQUARE_LOCATION_ID: str = ""
    # Webhook signature key (set in Square dashboard)
    SQUARE_WEBHOOK_SIGNATURE_KEY: str = ""

    # Superadmin seed (set in .env / Heroku config vars)
    SUPERADMIN_EMAIL: str = "admin@aistartupcopilot.org"
    SUPERADMIN_PASSWORD: str = ""
    SUPERADMIN_NAME: str = "Dr. David Noye"

    # App
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:3001"

    @property
    def cors_origins_list(self) -> List[str]:
        origins = [f"http://localhost:{p}" for p in range(3000, 3030)]
        if self.CORS_ORIGINS:
            for o in self.CORS_ORIGINS.split(","):
                o = o.strip()
                if o and o not in origins:
                    origins.append(o)
        return origins


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
