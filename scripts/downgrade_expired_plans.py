"""
Clariva — Expired plan downgrade (post-launch addendum, real-money payment
wiring).

Manual/offline entry point, same "connect to the DB directly, no FastAPI
request context" shape as migrate_db.py and scripts/run_monthly_billing.py
at the top of backend/.

Why this exists: Square's Payment Links API charges once — it is not a
real recurring subscription object on Square's side (there is no
auto-rebill next month). So this app is the sole source of truth for
whether an org's paid plan is still current. routers/payments.py's webhook
sets Organization.plan_expires_at = now + 30 (monthly) or 365 (annual)
days at the moment a plan checkout completes; this script is the other
half — it runs daily and flips any org whose plan_expires_at has passed
back to "free", clearing the column. The org sees a "renew" prompt (same
pricing.tsx checkout flow) rather than silently losing paid status with no
explanation, and nothing here ever touches an org's AI Services balance —
that's a completely separate ledger (see Organization.plan_expires_at's
docstring in models/db_models.py for why the two are deliberately not
coupled).

Deployment: meant to be invoked daily by Heroku's free Scheduler add-on,
alongside scripts/run_monthly_billing.py (two separate Scheduler jobs on
the same add-on — see docs/ARCHITECTURE.md):

    heroku addons:open scheduler --app <your-app>
    # Add Job -> "python scripts/downgrade_expired_plans.py" -> Daily

Usage (manual):
    cd backend
    python scripts/downgrade_expired_plans.py                              # local
    heroku run python scripts/downgrade_expired_plans.py --app atifixia-api  # production

Idempotency: safe to run any number of times per day. An org is only
touched if plan_expires_at is non-null AND in the past AND plan != "free"
(an org already on "free" is left alone even if a stale plan_expires_at
somehow lingers, since there's nothing to downgrade). Running this twice
in a row finds nothing to do the second time.

Failure isolation: one org's failure (should never happen — this is a
straight column write, no external calls) is logged and skipped rather
than aborting the batch, same precedent as run_monthly_billing.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("downgrade_expired_plans")


async def main() -> None:
    # Imported inside main() so this script's module-level docstring/CLI
    # help doesn't require the full app dependency stack importable at
    # parse time — same convention as migrate_db.py / run_monthly_billing.py.
    from sqlalchemy import select

    from audit import log_action
    from database import AsyncSessionLocal, settings
    from models.db_models import Organization

    target = settings.DATABASE_URL
    if "@" in target:  # don't print credentials embedded in a Postgres URL
        target = target.split("@", 1)[1]
    print(f"Clariva plan-expiry downgrade — target database: {target}")

    now = datetime.now(timezone.utc)
    downgraded, failed = 0, 0

    async with AsyncSessionLocal() as db:
        # Filter in Python rather than push `plan_expires_at < now` into the
        # SQL WHERE clause: this app's SQLite test/dev path hands back naive
        # datetimes on read for DateTime(timezone=True) columns while
        # PostgreSQL hands back tz-aware ones (see engines/award_engine.py's
        # _naive() helper and its two later duplicates in routers/
        # invitations.py and engines/alerts_engine.py — the exact same class
        # of bug, fixed three times already this project). Org count is
        # small (paid orgs only), so fetching plan != "free" and filtering
        # expiry in Python here avoids relying on the SQL dialect to compare
        # a bound aware `now` against a possibly-naive-on-read column
        # consistently.
        result = await db.execute(select(Organization).where(Organization.plan != "free"))
        candidates = result.scalars().all()

        def _naive(dt):
            return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo is not None else dt

        now_naive = _naive(now)
        orgs = [
            org for org in candidates
            if org.plan_expires_at is not None and _naive(org.plan_expires_at) < now_naive
        ]
        print(f"Found {len(orgs)} org(s) with an expired plan (out of {len(candidates)} on a paid plan).")

        for org in orgs:
            try:
                previous_plan = org.plan
                previous_seats = org.purchased_seats
                org.plan = "free"
                org.plan_expires_at = None
                # Purchased seats (Commercial Architecture Phase 1) are
                # billed against a specific paid tier's seat price — once
                # the org has no paid plan, those seats have nothing to
                # attach to. Reset rather than carry a stale count forward
                # to whatever tier the org resubscribes to later.
                org.purchased_seats = 0
                # System-initiated, not any one member's action — use the
                # org's creator as the audit actor (same "someone has to be
                # the actor" constraint audit.log_action enforces
                # everywhere else; there is no dedicated "system" user row
                # in this app).
                await log_action(
                    db, actor_id=org.created_by, action="billing.plan_expired", org_id=org.id,
                    object_type="organization", object_id=org.id,
                    detail={"previous_plan": previous_plan, "downgraded_to": "free", "seats_cleared": previous_seats},
                )
                await db.commit()
                downgraded += 1
                log.info("Downgraded org %s from %s to free (plan_expires_at had passed).", org.id, previous_plan)
            except Exception as exc:  # noqa: BLE001 — one org's failure must never abort the batch
                await db.rollback()
                failed += 1
                log.error("Failed to downgrade org %s: %s", org.id, exc)

    print(f"Done. Downgraded: {downgraded} | Failed: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
