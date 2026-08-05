"""RBAC permission registry — pure logic, no DB/HTTP involved."""
from __future__ import annotations

import rbac


def test_owner_has_every_permission_any_role_grants():
    all_permissions = set().union(*rbac.ROLE_PERMISSIONS.values())
    owner_permissions = rbac.ROLE_PERMISSIONS["owner"]
    assert all_permissions <= owner_permissions, (
        "every permission must be reachable by at least the owner role"
    )


def test_viewer_can_only_view_proposals():
    assert rbac.ROLE_PERMISSIONS["viewer"] == frozenset({"view_proposals"})


def test_editor_can_invite_and_share_but_not_manage_roles():
    assert rbac.role_has_permission("editor", "invite_members") is True
    assert rbac.role_has_permission("editor", "share_proposals") is True
    assert rbac.role_has_permission("editor", "manage_roles") is False
    assert rbac.role_has_permission("editor", "remove_members") is False


def test_role_has_permission_unknown_role_is_false():
    assert rbac.role_has_permission("not_a_real_role", "view_proposals") is False


def test_roles_with_permission_matches_registry():
    assert set(rbac.roles_with_permission("manage_roles")) == {"owner"}
    assert set(rbac.roles_with_permission("view_proposals")) == {"owner", "editor", "viewer"}


def test_roles_with_permission_returns_empty_for_unknown_permission():
    assert rbac.roles_with_permission("not_a_real_permission") == []


def test_is_valid_role():
    assert rbac.is_valid_role("owner") is True
    assert rbac.is_valid_role("superuser") is False
