"""Auth router — register, login, token refresh, MFA."""

import base64
import io
from datetime import datetime, timedelta
from typing import Annotated

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import settings
from database import get_db
from models.db_models import User
from models.schemas import (
    LoginResponse, MFADisableRequest, MFALoginRequest, MFASetupResponse,
    MFAVerifyRequest, UserCreate, UserLogin, TokenResponse, UserOut,
)
from audit import log_action

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

ALGORITHM = "HS256"
MFA_ISSUER = "Clariva"
MFA_CHALLENGE_EXPIRE_MINUTES = 5

# Use bcrypt directly to avoid passlib/bcrypt version incompatibility
import bcrypt as _bcrypt

def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_token(data: dict, expires_delta: timedelta) -> str:
    payload = {**data, "exp": datetime.utcnow() + expires_delta}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if not user_id:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    result = await db.execute(
        select(User).options(selectinload(User.org_context)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_exc
    return user


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    now = datetime.utcnow()
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        organization=body.organization,
        created_at=now,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        organization=user.organization,
        created_at=user.created_at or now,
    )


def _issue_tokens(user: User) -> LoginResponse:
    access_token = create_token(
        {"sub": user.id, "email": user.email},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_token(
        {"sub": user.id, "type": "refresh"},
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    return LoginResponse(access_token=access_token, refresh_token=refresh_token, mfa_required=False)


@router.post("/login", response_model=LoginResponse)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if user.mfa_enabled:
        # Password verified, but the login isn't complete until the caller
        # posts a valid TOTP code to /auth/mfa/login with this short-lived
        # challenge token. No access/refresh tokens are issued yet.
        mfa_token = create_token(
            {"sub": user.id, "type": "mfa_challenge"},
            timedelta(minutes=MFA_CHALLENGE_EXPIRE_MINUTES),
        )
        return LoginResponse(mfa_required=True, mfa_token=mfa_token)

    return _issue_tokens(user)


@router.post("/mfa/login", response_model=LoginResponse)
async def mfa_login(
    body: MFALoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Second step of login for users with MFA enabled — exchanges the
    mfa_token from /auth/login plus a valid TOTP code for real tokens."""
    try:
        payload = jwt.decode(body.mfa_token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "mfa_challenge":
            raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge.")
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge.")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.mfa_enabled or not user.mfa_secret:
        raise HTTPException(status_code=401, detail="Invalid or expired MFA challenge.")

    if not pyotp.TOTP(user.mfa_secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid verification code.")

    return _issue_tokens(user)


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


from pydantic import BaseModel, EmailStr
from typing import Optional

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    organization: Optional[str] = None

class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.full_name is not None:
        current_user.full_name = body.full_name
    if body.organization is not None:
        current_user.organization = body.organization
    if body.email is not None and body.email != current_user.email:
        clash = await db.execute(select(User).where(User.email == body.email))
        if clash.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already in use")
        current_user.email = body.email
    await db.flush()
    await db.refresh(current_user)
    return current_user


@router.patch("/me/password", status_code=204)
async def change_password(
    body: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current_user.hashed_password = hash_password(body.new_password)
    await db.flush()


# ── MFA (TOTP) — Clariva Enterprise™ PRD §18 ─────────────────────────────────

def _qr_code_base64(otpauth_uri: str) -> str:
    img = qrcode.make(otpauth_uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@router.post("/mfa/setup", response_model=MFASetupResponse)
async def setup_mfa(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates a new TOTP secret and stores it on the user (mfa_enabled
    stays False until /mfa/verify confirms the user actually scanned it and
    can produce a valid code — otherwise a typo'd or never-scanned secret
    would lock the user out).
    """
    secret = pyotp.random_base32()
    current_user.mfa_secret = secret
    await db.flush()

    otpauth_uri = pyotp.TOTP(secret).provisioning_uri(name=current_user.email, issuer_name=MFA_ISSUER)
    return MFASetupResponse(
        secret=secret,
        otpauth_uri=otpauth_uri,
        qr_code_png_base64=_qr_code_base64(otpauth_uri),
    )


@router.post("/mfa/verify", response_model=UserOut)
async def verify_mfa(
    body: MFAVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Confirms a code from the authenticator app and turns MFA on."""
    if not current_user.mfa_secret:
        raise HTTPException(status_code=400, detail="Call /auth/mfa/setup first.")
    if not pyotp.TOTP(current_user.mfa_secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    current_user.mfa_enabled = True
    await db.flush()
    await log_action(db, actor_id=current_user.id, action="mfa.enabled",
                      object_type="user", object_id=current_user.id)
    return current_user


@router.post("/mfa/disable", status_code=204)
async def disable_mfa(
    body: MFADisableRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if current_user.mfa_enabled and current_user.mfa_secret:
        # If already enabled, also require a valid code — password alone
        # (e.g. a stolen session + known password) shouldn't be enough to
        # turn off a second factor.
        if not body.code or not pyotp.TOTP(current_user.mfa_secret).verify(body.code, valid_window=1):
            raise HTTPException(status_code=400, detail="Invalid verification code.")

    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    await db.flush()
    await log_action(db, actor_id=current_user.id, action="mfa.disabled",
                      object_type="user", object_id=current_user.id)
