"""Contract lock for the 客戶 list endpoint (POST /contacts/query).

The admin SPA's customers table reads `channel_identities` / `tags` /
`one_id` / `last_active_at` straight off every list row. An earlier build
returned bare ContactOut rows without those keys and the WHOLE page crashed
(`undefined.slice` → global ErrorBoundary) — the third shape-drift incident
in this repo. These tests pin the response schema so a revert to the flat
serializer fails CI instead of production.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from apps.api.app.modules.contacts.router import (
    ContactListItemOut,
    ContactListOut,
    ContactOut,
)

# Keys the SPA's CustomersPage/endpoints.ts consume on every list row.
SPA_ROW_KEYS = {
    "id", "display_name", "email", "phone", "is_blacklisted", "custom",
    "channel_identities", "tags", "one_id", "assignee_member_id",
    "assignee_name", "last_active_at", "created_at",
}


def _contact_row() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        display_name="測試客戶",
        remark_name=None,
        avatar_url=None,
        email="a@b.c",
        phone=None,
        language="zh-Hant",
        country=None,
        city=None,
        timezone=None,
        custom={},
        is_blacklisted=False,
        merged_into_id=None,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
    )


def test_list_item_carries_every_spa_key_with_safe_defaults():
    item = ContactListItemOut.model_validate(_contact_row())
    dumped = item.model_dump()
    missing = SPA_ROW_KEYS - dumped.keys()
    assert not missing, f"list row lost SPA-consumed keys: {missing}"
    # the two array fields must NEVER be absent/None — undefined.slice was the
    # production crash
    assert dumped["channel_identities"] == []
    assert dumped["tags"] == []


def test_query_response_model_uses_enriched_items():
    # If someone reverts ContactListOut.items to bare ContactOut, the endpoint
    # silently drops the enrichment again — fail here instead.
    ann = str(ContactListOut.model_fields["items"].annotation)
    assert "ContactListItemOut" in ann
    assert issubclass(ContactListItemOut, ContactOut)
