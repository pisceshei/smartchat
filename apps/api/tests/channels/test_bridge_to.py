"""_bridge_to — outbound recipient addressing for whatsapp_app lid identities.
An identity still keyed by an unresolved lid must be addressed at @lid
explicitly; healed (phone-keyed) identities send bare digits as before."""
from __future__ import annotations

from types import SimpleNamespace

from apps.api.app.channels.sender import _bridge_to

LID = "56985642876983"
PHONE = "85266577437"


def _identity(external: str, meta: dict | None = None):
    return SimpleNamespace(external_user_id=external, meta=meta or {})


def test_unresolved_lid_identity_addresses_lid_server():
    ident = _identity(LID, {"wa_lid": LID})
    assert _bridge_to(ident, "whatsapp_app") == f"{LID}@lid"


def test_healed_phone_identity_sends_bare_digits():
    ident = _identity(PHONE, {"wa_lid": LID})  # migrated: key is the phone now
    assert _bridge_to(ident, "whatsapp_app") == PHONE


def test_identity_without_lid_meta_unchanged():
    assert _bridge_to(_identity(PHONE), "whatsapp_app") == PHONE
    assert _bridge_to(_identity(PHONE, None), "whatsapp_app") == PHONE


def test_other_channels_never_rewrite():
    ident = _identity(LID, {"wa_lid": LID})
    assert _bridge_to(ident, "whatsapp_cloud") == LID
    assert _bridge_to(ident, "telegram_bot") == LID
