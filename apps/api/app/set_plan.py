"""Self-use plan provisioning: set a registered user's workspace to a plan with
no charge (same effect as a paid Stripe webhook). For the chilling.com.hk
self-use account — register at the site first, then run this once.

    python -m apps.api.app.set_plan <email> [plan_code] [duration_days]

Defaults: plan_code=max, duration_days=720. Idempotent; safe to re-run.
Exit codes: 0 ok, 1 user/workspace not found, 2 usage error.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from .db import session_factory
from .models.members import User
from .models.tenancy import Workspace
from .modules.billing import service
from .services.redis_client import get_redis


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m apps.api.app.set_plan <email> [plan_code=max] [duration_days=720]")
        raise SystemExit(2)
    email = sys.argv[1].strip()
    plan_code = sys.argv[2] if len(sys.argv) > 2 else "max"
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 720

    async with session_factory()() as session:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is None:
            print(f"NOT_FOUND: no user with email {email!r} — register at the site first")
            raise SystemExit(1)
        ws = (
            await session.execute(
                select(Workspace).where(Workspace.owner_user_id == user.id)
            )
        ).scalars().first()
        if ws is None:
            print(f"NOT_FOUND: user {email!r} owns no workspace")
            raise SystemExit(1)

        redis = get_redis()
        await service.admin_change_plan(
            session,
            redis,
            workspace_id=ws.id,
            plan_code=plan_code,
            duration_days=duration,
            addons={"seats": 0, "official_channels": 0, "hosted_devices": 0},
        )
        await session.commit()
        print(f"OK: workspace {ws.name!r} ({ws.id}) -> plan={plan_code} duration={duration}d")


if __name__ == "__main__":
    asyncio.run(main())
