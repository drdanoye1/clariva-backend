"""
Clariva Intelligent Grant Writing Platform — Configuration
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

    # Funding Intelligence (Phase 4, PRD §15/§19) — Grants.gov's search2/
    # fetchOpportunity APIs are public and need no key. SAM.gov's opportunity
    # API does; sync_sam_gov() gracefully no-ops (reports "not configured")
    # when this is left blank, same "degrade gracefully" pattern as every
    # other optional integration in this codebase.
    SAM_GOV_API_KEY: str = ""

    # Database — Neon PostgreSQL (set in .env / Heroku config vars)
    DATABASE_URL: str = "sqlite+aiosqlite:///./sbir_platform.db"   # fallback for local-only dev
    SYNC_DATABASE_URL: str = ""

    # File storage — Cloudflare R2 (Version 3.0 upgrade, "Real File Storage"
    # scope; see docs/Clariva_File_Storage_Scoping_Document.docx). R2 exposes
    # a fully S3-compatible API, so storage.py talks to it via boto3's S3
    # client pointed at R2's endpoint rather than a Cloudflare-specific SDK.
    # Replaces a dead, never-wired-up SUPABASE_* block that lived here
    # before — grepping the rest of the backend for SUPABASE_ turned up zero
    # other references, confirming it was leftover from an earlier,
    # abandoned storage plan.
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
    # Optional: only needed if the bucket is ever fronted by a public/CDN
    # hostname for unauthenticated assets (e.g. white-label logos) — not
    # used by the presigned-URL flow storage.py implements today.
    R2_PUBLIC_HOSTNAME: str = ""

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
    SQUARE_APP_NAME: str = "Clariva"
    # Optional: override the auto-detected Square location ID
    SQUARE_LOCATION_ID: str = ""
    # Webhook signature key (set in Square dashboard)
    SQUARE_WEBHOOK_SIGNATURE_KEY: str = ""
    # The exact notification URL configured for this webhook subscription in
    # the Square dashboard (Developer Dashboard -> Webhooks -> Subscription
    # -> Notification URL). Square's HMAC signature is computed over
    # (this URL + raw request body), so it must match byte-for-byte —
    # including scheme and trailing slash/no-slash — or every signature
    # check fails. Can't be derived from the incoming request reliably
    # (Heroku's router rewrites scheme/host headers), so it's a separate,
    # explicitly-set config var. e.g.
    # "https://atifixia-api.herokuapp.com/api/v1/payments/webhook"
    SQUARE_WEBHOOK_NOTIFICATION_URL: str = ""

    # Transactional email (Resend) — used to send org-member invitation
    # emails (routers/invitations.py). Degrades gracefully exactly like
    # SAM_GOV_API_KEY: when RESEND_API_KEY is blank, email_service.send_email()
    # logs a warning and returns False instead of raising, so an invitation
    # can still be created (and its link relayed by other means) before a
    # Resend account is set up.
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "Clariva <onboarding@resend.dev>"
    # Base URL used to build the invite-accept link embedded in emails
    # (e.g. "https://app.clariva.ai") — set this in Heroku config vars for
    # production; the localhost default only matters for local dev.
    FRONTEND_URL: str = "http://localhost:3000"

    # PDF export (Word/PDF Report Generation Development Specification,
    # CLARIVA-DOCGEN-SPEC-001, Phase 8) — headless LibreOffice converts the
    # already-rendered DOCX to PDF, so PDF export reflects the exact same
    # structured-block content (figures, tables, schedules, callouts,
    # references) as DOCX export instead of a second, drifted-apart
    # reportlab renderer. Left blank, utils/pdf_convert.py auto-detects the
    # `soffice` (or `libreoffice`) binary via shutil.which() on PATH — this
    # override only matters for an environment where the binary exists but
    # isn't named/located where PATH lookup finds it. Degrades gracefully
    # exactly like SAM_GOV_API_KEY/RESEND_API_KEY: if no binary is found
    # (or the conversion fails for any reason), export falls back to the
    # legacy reportlab PDF renderer rather than failing the export.
    SOFFICE_BINARY: str = ""

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
