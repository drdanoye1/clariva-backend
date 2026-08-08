"""
Clariva — Role-Based Permissions registry (Clariva Enterprise™ PRD §12).

This formalizes the roles that already existed as free-form strings on
`OrgMembership.role` ("owner", "editor", "viewer") into an explicit
role -> permission-set registry, and generalizes the ad-hoc
`_assert_role(org_id, user_id, ["owner", "editor"], db)` checks scattered
across routers/organizations.py into a single `_assert_permission()` call
per action.

This is deliberately NOT a dynamic, database-backed Role/Permission table
yet — that's the natural next step (per docs/ARCHITECTURE.md) once a
customer actually needs custom roles beyond owner/editor/viewer. A static
registry is the smallest change that turns implicit, per-endpoint role
lists into one explicit, testable source of truth, without restructuring
data that's already in production.

To add a new permission: add it to the relevant role sets below, then use
`role_has_permission()` / `roles_with_permission()` wherever an endpoint
needs to gate on it. Never remove a permission from "owner" — every
permission must be reachable by at least the owner role.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List

ROLES: tuple[str, ...] = ("owner", "editor", "viewer")

ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    "owner": frozenset({
        "invite_members",
        "remove_members",
        "manage_roles",
        "share_proposals",
        "view_proposals",
        "manage_org",
        "view_audit_log",
        "manage_credits",       # top up / allocate the shared AI credit pool
        # Phase 3 — Collaboration & Content Management (PRD §11, §14)
        "manage_workspace",     # create/edit/delete departments & teams
        "manage_guests",        # invite/revoke external guest access
        "assign_tasks",         # create/assign workspace tasks
        "manage_approvals",     # decide any approval request, not just ones assigned to you
        "manage_documents",     # create documents / publish new versions
        "manage_document_sharing",  # create/revoke document share links
        "manage_retention",     # set retention policies, run archive-expired
        # Phase 4 — Funding Intelligence & Grant Tracking (PRD §15)
        "manage_pipeline",      # change pipeline stage / bid-no-go on org-shared opportunities
        "manage_watchlists",    # create/edit/delete watchlists, trigger Grants.gov/SAM.gov sync
        # Phase 5 — Award & Project Management (PRD §16-17)
        "manage_awards",        # create/edit awards, record expenditures, compliance items, issues, performance, closeout, renewals
        # Phase 7 — Award Received data model foundation (Version 3.0 upgrade)
        "activate_award",       # lock a ProjectBaseline and flip an award from "received" to "active" — kept
                                 # as its own permission (not folded into manage_awards) since it's a one-way,
                                 # baseline-locking action; enforcement today is still via workspace_access.py's
                                 # assert_can_edit (same as manage_awards — see routers/awards.py's module
                                 # docstring), this registry entry exists for the same "documented, not yet
                                 # actively branched on" reason manage_awards does.
        # Phase 6 — Integrations & Marketplace (PRD §19-20)
        "manage_connectors",    # create/edit/delete/test connector connections (webhooks, Slack, etc.) and view their event log
        "manage_api_keys",      # issue/revoke public API keys
        "manage_branding",      # configure white-label branding (logo, brand name, primary color)
        "manage_marketplace_listings",  # publish/edit/archive this org's marketplace listings
    }),
    "editor": frozenset({
        "invite_members",
        "share_proposals",
        "view_proposals",
        # Phase 3
        "assign_tasks",
        "manage_documents",
        "manage_document_sharing",
        # Phase 4
        "manage_pipeline",
        "manage_watchlists",
        # Phase 5
        "manage_awards",
        # Phase 7
        "activate_award",
        # Phase 6 permissions are deliberately owner-only — connector
        # credentials, API keys, branding, and marketplace listings are all
        # organization-identity/security-sensitive, unlike day-to-day
        # collaboration work editors already do.
    }),
    "viewer": frozenset({
        "view_proposals",
    }),
}


def role_has_permission(role: str, permission: str) -> bool:
    """True if the given role grants the given permission."""
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def roles_with_permission(permission: str) -> List[str]:
    """Every role that grants the given permission, in ROLES order."""
    return [r for r in ROLES if permission in ROLE_PERMISSIONS.get(r, frozenset())]


def is_valid_role(role: str) -> bool:
    return role in ROLE_PERMISSIONS
