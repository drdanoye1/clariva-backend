"""
AtiFixia SBIR Intelligence Platform — FastAPI Application
Entry point: uvicorn main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import create_tables
from routers import (
    auth, proposals, foa, scoring, reviewer,
    documents, memory, organizations, profile,
    extract, budget, budget_export, payments, admin,
)
from routers import suggest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _seed_superadmin() -> None:
    """Create the superadmin user if it doesn't exist yet."""
    if not settings.SUPERADMIN_PASSWORD:
        logger.warning("SUPERADMIN_PASSWORD not set — skipping superadmin seed.")
        return
    from database import AsyncSessionLocal
    from models.db_models import User
    from routers.auth import hash_password
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == settings.SUPERADMIN_EMAIL))
        existing = result.scalar_one_or_none()
        if existing:
            # Ensure superadmin flag is set (in case of migration)
            if not existing.is_superadmin:
                existing.is_superadmin = True
                existing.role = "superadmin"
                existing.subscription_plan = "enterprise"
                await db.commit()
                logger.info("Superadmin flags updated for %s", settings.SUPERADMIN_EMAIL)
            return
        user = User(
            email=settings.SUPERADMIN_EMAIL,
            hashed_password=hash_password(settings.SUPERADMIN_PASSWORD),
            full_name=settings.SUPERADMIN_NAME,
            organization="NanoResearch, Inc",
            is_active=True,
            is_superadmin=True,
            role="superadmin",
            subscription_plan="enterprise",
        )
        db.add(user)
        await db.commit()
        logger.info("Superadmin created: %s", settings.SUPERADMIN_EMAIL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting AtiFixia SBIR Intelligence Platform...")
    await create_tables()
    logger.info("Database tables verified.")
    await _seed_superadmin()
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="AtiFixia SBIR Intelligence Platform",
    description="Governance-Driven AI Proposal Intelligence — Multi-Grant-Type Support",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,          prefix="/api/v1/auth",          tags=["Auth"])
app.include_router(foa.router,           prefix="/api/v1/foa",           tags=["FOA"])
app.include_router(proposals.router,     prefix="/api/v1/proposals",     tags=["Proposals"])
app.include_router(scoring.router,       prefix="/api/v1/scoring",       tags=["Scoring"])
app.include_router(reviewer.router,      prefix="/api/v1/reviewer",      tags=["Reviewer Simulation"])
app.include_router(documents.router,     prefix="/api/v1/documents",     tags=["Document Export"])
app.include_router(memory.router,        prefix="/api/v1/memory",        tags=["Memory & KPI"])
app.include_router(organizations.router, prefix="/api/v1/organizations", tags=["Organizations"])
app.include_router(profile.router,       prefix="/api/v1/profile",       tags=["Company Profile"])
app.include_router(extract.router,       prefix="/api/v1/extract",       tags=["Document Extraction"])
app.include_router(budget.router,        prefix="/api/v1/budget",        tags=["Budget Builder"])
app.include_router(budget_export.router, prefix="/api/v1/budget",        tags=["Budget Export"])
app.include_router(payments.router,      prefix="/api/v1/payments",      tags=["Payments"])
app.include_router(admin.router,         prefix="/api/v1/admin",         tags=["Admin"])
app.include_router(suggest.router,       prefix="/api/v1/suggest",       tags=["AI Suggestions"])


@app.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "ok",
        "version": "2.0.0",
        "platform": "AtiFixia SBIR Intelligence Platform",
    }


@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "AtiFixia SBIR Intelligence Platform API",
        "docs": "/docs",
        "version": "2.0.0",
    }
