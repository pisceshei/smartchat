"""AI member create defaults: 轉人工 escalation keywords are seeded once at
create time and tenant-provided rules are never overwritten."""
from __future__ import annotations

import uuid

from apps.api.app.modules.ai.service import create_ai_member


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


async def _create(escalation_rules: dict) -> tuple:
    return await create_ai_member(
        FakeSession(),
        workspace_id=uuid.uuid4(),
        name="小智",
        persona={},
        model_tier="smart",
        kb_collection_ids=[],
        skills=[],
        monthly_msg_quota=0,
        mode="builtin",
        external={},
        escalation_rules=escalation_rules,
        max_concurrent=0,
        role_id=None,
    )


async def test_create_seeds_default_handoff_keywords():
    _, agent = await _create({})
    assert agent.escalation_rules["keywords"] == ["真人", "人工", "human"]


async def test_create_empty_keyword_list_gets_defaults():
    _, agent = await _create({"keywords": [], "max_kb_miss": 5})
    assert agent.escalation_rules["keywords"] == ["真人", "人工", "human"]
    assert agent.escalation_rules["max_kb_miss"] == 5  # other rules preserved


async def test_create_keeps_tenant_keywords():
    _, agent = await _create({"keywords": ["轉接客服"]})
    assert agent.escalation_rules["keywords"] == ["轉接客服"]
