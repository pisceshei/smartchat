"""Seed the database: plans fixture (idempotent upsert). Run after alembic:

    python -m apps.api.app.seed
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import session_factory
from .models.tenancy import Plan

log = logging.getLogger("smartchat.seed")

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_plans_fixture() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "plans.json").read_text(encoding="utf-8"))["plans"]


async def seed_plans(session: AsyncSession) -> int:
    """Upsert plans; existing rows get refreshed limits/pricing."""
    plans = load_plans_fixture()
    for p in plans:
        stmt = pg_insert(Plan).values(
            code=p["code"],
            name=p["name"],
            price_usd_month=p["price_usd_month"],
            limits=p["limits"],
            sort_order=p.get("sort_order", 0),
            is_active=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={
                "name": stmt.excluded.name,
                "price_usd_month": stmt.excluded.price_usd_month,
                "limits": stmt.excluded.limits,
                "sort_order": stmt.excluded.sort_order,
            },
        )
        await session.execute(stmt)
    return len(plans)


async def ensure_plan(session: AsyncSession, code: str) -> None:
    """Make sure a plan row exists (used by auth.register so a fresh install
    works even before the seed script ran)."""
    if await session.get(Plan, code) is not None:
        return
    for p in load_plans_fixture():
        if p["code"] == code:
            session.add(
                Plan(
                    code=p["code"],
                    name=p["name"],
                    price_usd_month=p["price_usd_month"],
                    limits=p["limits"],
                    sort_order=p.get("sort_order", 0),
                )
            )
            return
    raise ValueError(f"unknown plan code: {code}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with session_factory()() as session:
        n = await seed_plans(session)
        await session.commit()
    log.info("seeded %d plans", n)


if __name__ == "__main__":
    asyncio.run(main())
