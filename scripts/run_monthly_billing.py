"""
Clariva — Post-Award Management recurring monthly billing (Phase 3 §4.7
billing wire-up).

Manual/offline entry point, same "connect to the DB directly, no FastAPI
request context" shape as migrate_db.py at the top of backend/ — this is
NOT wired into any HTTP route, because Active Award Management &
Compliance (post_award_management_standard/advanced/complex, $75/$150/
$250 per month per active award) is genuinely recurring, and this app has
no background scheduler/cron of its own (confirmed at the time this was
built — no APScheduler/Celery/RQ dependency, no `worker`/`clock` process
in the Procfile; see docs/ARCHITECTURE.md).

Deployment: this script is meant to be invoked on a monthly cadence by
Heroku's free Scheduler add-on (a one-off dyno on a cron schedule, no
code required beyond an entrypoint command) — NOT set up automatically by
this codebase. To enable it in production:

    heroku addons:create scheduler:standard --app <your-app>
    heroku addons:open scheduler --app <your-app>
    # In the dashboard: Add Job -> "python scripts/run_monthly_billing.py"
    # -> Frequency: Monthly (or Daily; this script is itself idempotent
    # per-award-per-month, so running it more often than monthly is safe,
    # just wasteful — see _due_for_billing() below).

Usage (manual, e.g. to bill immediately without waiting for the
scheduler):
    cd backend
    python scripts/run_monthly_billing.py                              # local
    heroku run python scripts/run_monthly_billing.py --app atifixia-api  # production

What gets billed: every Award with status == "active" (not closed/
terminated), award_status == "active" (i.e. has actually been through
Activate Project — see routers/awards.py::activate_award), a non-null
org_id (personal/unshared awards are never billed, same "org-scoped only"
rule as every other paid feature in this app), and a non-null
post_award_tier (set once at activation from the award's total dollar
value — see routers/awards.py::_post_award_tier_from_value). An award
that predates this column entirely (post_award_tier is NULL) is SKIPPED,
not defaulted to "standard" — silently guessing a dollar-driven tier for
an award this script has no basis to assess would be worse than not
billing it at all; an operator can backfill post_award_tier by hand via
a direct DB update if this ever needs to apply retroactively.

Idempotency: each award is billed at most once per calendar month, judged
by comparing Award.post_award_last_billed_at's (year, month) to the
current UTC (year, month) — not a rolling 30-day window, so a slightly
early or late scheduler run still lands in the right "billing period"
bucket rather than drifting. Running this script twice in the same month
is always safe: the second run finds every award already billed for that
month and does nothing.

Failure isolation: one award's InsufficientCreditsError (a drained AI
Services balance) is logged and skipped — it does NOT stop the rest of
the batch from being billed, same "one bad item doesn't abort the whole
run" precedent as routers/foa.py's bulk-analyze endpoint.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("run_monthly_billing")


def _due_for_billing(award, now: datetime) -> bool:
    """True if `award` has not yet been billed for the (year, month) `now`
    falls in."""
    last = award.post_award_last_billed_at
    if last is None:
        return True
    return (last.year, last.month) != (now.year, now.month)


async def main() -> None:
    # Imported inside main() so this script's module-level docstring/CLI
    # help doesn't require the full app dependency stack importable at
    # parse time — same convention as migrate_db.py.
    from sqlalchemy import select

    from database import AsyncSessionLocal, settings
    from models.db_models import Award
    from engines.credit_engine import InsufficientCreditsError
    from engines.service_catalog_engine import ServiceCatalogEngine

    target = settings.DATABASE_URL
    if "@" in target:  # don't print credentials embedded in a Postgres URL
        target = target.split("@", 1)[1]
    print(f"Clariva monthly billing — target database: {target}")

    catalog_engine = ServiceCatalogEngine()
    now = datetime.now(timezone.utc)

    billed, skipped_no_tier, skipped_already_billed, failed = 0, 0, 0, 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Award).where(
                Award.status == "active",
                Award.award_status == "active",
                Award.org_id.isnot(None),
            )
        )
        awards = result.scalars().all()
        print(f"Found {len(awards)} active, org-scoped award(s) to evaluate.")

        for award in awards:
            if not award.post_award_tier:
                skipped_no_tier += 1
                continue
            if not _due_for_billing(award, now):
                skipped_already_billed += 1
                continue

            service_key = f"post_award_management_{award.post_award_tier}"
            try:
                await catalog_engine.consume(
                    db, award.org_id, user_id=None, service_key=service_key,
                    reference={"award_id": award.id, "billing_period": now.strftime("%Y-%m")},
                )
                award.post_award_last_billed_at = now
                await db.commit()
                billed += 1
                log.info("Billed award %s (%s, org %s).", award.id, service_key, award.org_id)
            except InsufficientCreditsError as exc:
                await db.rollback()
                failed += 1
                log.warning("Skipped award %s — insufficient AI Services balance: %s", award.id, exc)
            except Exception as exc:  # noqa: BLE001 — one award's failure must never abort the batch
                await db.rollback()
                failed += 1
                log.error("Skipped award %s — unexpected error: %s", award.id, exc)

    print(
        f"Done. Billed: {billed} | Already billed this month: {skipped_already_billed} | "
        f"No tier (skipped): {skipped_no_tier} | Failed: {failed}"
    )


if __name__ == "__main__":
    asyncio.run(main())
