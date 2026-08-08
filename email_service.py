"""
Transactional email via Resend — currently used only for org-member
invitations (routers/invitations.py, routers/organizations.py). Kept as a
standalone utility module rather than an engine, mirroring api_keys.py's
"plain utility, not an engine" role: it has no DB model of its own and no
router of its own list-and-CRUD shape.

Design notes:
- Degrades gracefully exactly like SAM_GOV_API_KEY (see config.py): when
  RESEND_API_KEY is blank, send_email() logs a warning and returns False
  instead of raising, so an Invitation row can still be created — and its
  link relayed by other means — before a Resend account is configured.
- `_post()` is the one seam that makes an actual HTTP call, matching
  connector_engine.py's convention — tests can monkeypatch it to assert on
  the call without hitting the real network.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import settings

_log = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


async def _post(payload: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        )


async def send_email(to: str, subject: str, html: str) -> bool:
    """Best-effort send — returns False (and logs) on any failure rather
    than raising, so a caller creating an Invitation never has that DB
    write rolled back just because the email provider hiccupped."""
    if not settings.RESEND_API_KEY:
        _log.warning("RESEND_API_KEY not configured — skipping email to %s (subject: %r)", to, subject)
        return False
    try:
        resp = await _post({"from": settings.EMAIL_FROM, "to": [to], "subject": subject, "html": html})
        resp.raise_for_status()
        return True
    except Exception:
        _log.exception("Failed to send email to %s", to)
        return False


def render_invitation_email(
    org_name: str, inviter_name: str, role: str, accept_url: str, team_name: Optional[str] = None,
) -> str:
    scope_line = f" on the <strong>{team_name}</strong> team" if team_name else ""
    article = "an" if role[:1].lower() in "aeiou" else "a"
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
      <h2 style="color:#0f172a; margin-bottom: 4px;">You're invited to join {org_name} on Clariva</h2>
      <p style="color:#334155; line-height: 1.6;">
        {inviter_name} has invited you to join <strong>{org_name}</strong> as {article} <strong>{role}</strong>{scope_line}
        on Clariva Enterprise&trade;, the AI-powered grant intelligence and lifecycle management platform.
      </p>
      <p style="margin: 28px 0;">
        <a href="{accept_url}" style="background:#1d4ed8; color:#fff; padding:12px 24px; border-radius:8px; text-decoration:none; font-weight:600; display:inline-block;">
          Accept Invitation
        </a>
      </p>
      <p style="color:#94a3b8; font-size:13px;">
        This invite link expires in 7 days. If you weren't expecting this, you can safely ignore this email.
      </p>
    </div>
    """
