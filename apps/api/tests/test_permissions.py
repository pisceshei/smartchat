"""RBAC permission matching semantics."""
from __future__ import annotations

from apps.api.app.deps import PERMISSION_KEYS, has_permission


def test_exact_match():
    assert has_permission({"inbox.view_all"}, "inbox.view_all")
    assert not has_permission({"inbox.view_all"}, "inbox.reply")


def test_star_grants_everything():
    for key in PERMISSION_KEYS:
        assert has_permission({"*"}, key)


def test_module_wildcard():
    perms = {"inbox.*", "contacts.view"}
    assert has_permission(perms, "inbox.reply")
    assert has_permission(perms, "inbox.view_all")
    assert has_permission(perms, "contacts.view")
    assert not has_permission(perms, "contacts.edit")
    assert not has_permission(perms, "settings.manage")


def test_empty_denies():
    assert not has_permission(set(), "inbox.view_mine")


def test_list_input_accepted():
    assert has_permission(["inbox.reply"], "inbox.reply")


def test_no_prefix_confusion():
    """"inbox.*" must not grant "inboxadmin.x" and vice versa."""
    assert not has_permission({"inbox.*"}, "inboxadmin.x")
    assert not has_permission({"in.*"}, "inbox.reply")
