"""
Clariva — shared proposal-access resolver for Phase 3 Collaboration &
Content Management (Clariva Enterprise™ PRD §11, §14).

Existing routers (proposals.py, scope_of_work.py) restrict editing to the
proposal's owner only (`_get_proposal_or_404`) — deliberately left
untouched here for backward compatibility (see docs/ARCHITECTURE.md §8).
But collaboration features (comments, tasks, document sharing) inherently
need to work for other people looking at a shared proposal: org members it
was shared to via OrgProposal, and now external guests scoped to that one
proposal via WorkspaceGuestAccess. This module is the one place that
resolves "can this user see/act on this proposal" across all three cases,
so routers/collaboration.py and routers/documents_library.py don't each
reimplement it slightly differently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db_models import OrgMembership, OrgProposal, Proposal, WorkspaceGuestAccess


@dataclass
class ProposalAccess:
    proposal: Proposal
    org_id: Optional[str]   # the org this access was resolved through, if any
    role: str               # "owner" | an OrgMembership role | "guest"
    can_edit: bool
    can_comment: bool


async def resolve_proposal_access(proposal_id: str, user_id: str, db: AsyncSession) -> ProposalAccess:
    """
    Raises 404 if the proposal doesn't exist, 403 if this user has no access
    to it at all. Otherwise returns what kind of access they have, checked
    in order: ownership, org-sharing, scoped guest invite.
    """
    result = await db.execute(select(Proposal).where(Proposal.id == proposal_id))
    proposal = result.scalar_one_or_none()
    if not proposal:
        raise HTTPException(status_code=404, detail="Proposal not found")

    if proposal.owner_id == user_id:
        # The owner may ALSO be an org member the proposal has been shared
        # with (sharing requires the sharer to hold "share_proposals" on
        # that org, so this is the common case, not an edge case) — resolve
        # that org_id too, so owner-created tasks/comments/approvals on a
        # shared proposal land in that org's workspace instead of being
        # permanently stranded with org_id=None. Falls back to None for a
        # genuinely personal, unshared proposal.
        org_id = None
        owned_share = await db.execute(
            select(OrgProposal.org_id)
            .join(OrgMembership, OrgMembership.org_id == OrgProposal.org_id)
            .where(OrgProposal.proposal_id == proposal_id, OrgMembership.user_id == user_id)
        )
        owned_row = owned_share.first()
        if owned_row:
            org_id = owned_row[0]
        return ProposalAccess(proposal=proposal, org_id=org_id, role="owner", can_edit=True, can_comment=True)

    shared = await db.execute(
        select(OrgProposal.org_id, OrgMembership.role)
        .join(OrgMembership, OrgMembership.org_id == OrgProposal.org_id)
        .where(OrgProposal.proposal_id == proposal_id, OrgMembership.user_id == user_id)
    )
    row = shared.first()
    if row:
        org_id, role = row
        can_edit = role in ("owner", "editor")
        return ProposalAccess(proposal=proposal, org_id=org_id, role=role, can_edit=can_edit, can_comment=True)

    guest = await db.execute(
        select(WorkspaceGuestAccess).where(
            WorkspaceGuestAccess.proposal_id == proposal_id,
            WorkspaceGuestAccess.user_id == user_id,
        )
    )
    guest_row = guest.scalar_one_or_none()
    if guest_row:
        return ProposalAccess(
            proposal=proposal, org_id=guest_row.org_id, role="guest",
            can_edit=False, can_comment=guest_row.can_comment,
        )

    raise HTTPException(status_code=403, detail="You do not have access to this proposal.")


async def assert_can_view(proposal_id: str, user_id: str, db: AsyncSession) -> ProposalAccess:
    return await resolve_proposal_access(proposal_id, user_id, db)


async def assert_can_comment(proposal_id: str, user_id: str, db: AsyncSession) -> ProposalAccess:
    access = await resolve_proposal_access(proposal_id, user_id, db)
    if not access.can_comment:
        raise HTTPException(status_code=403, detail="You do not have comment access to this proposal.")
    return access


async def assert_can_edit(proposal_id: str, user_id: str, db: AsyncSession) -> ProposalAccess:
    access = await resolve_proposal_access(proposal_id, user_id, db)
    if not access.can_edit:
        raise HTTPException(status_code=403, detail="You do not have edit access to this proposal.")
    return access
