"""whatsapp_app / line_app QR provisioning.

Pure tests assert the connect wiring (whatsapp_app is connectable + routed to the
bridge flow, adapter registered, status mapping). The live test (auto-skips if
pg/redis are down) drives provision_device with a mocked bridge and asserts the
account + device_bridge + encrypted credential + bridge_url wiring, the graceful
degraded path when the bridge is offline, and the qr/status/logout lifecycle.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import apps.api.app.db as dbmod
from apps.api.app.channels.creds import get_credentials
from apps.api.app.channels.registry import get_adapter
from apps.api.app.channels.sender import pause_key
from apps.api.app.deps import MemberContext
from apps.api.app.models.channels import ChannelAccount, DeviceBridge
from apps.api.app.models.tenancy import Workspace
from apps.api.app.modules.channels import router as ch_router
from apps.api.app.modules.devices import service as dsvc
from apps.api.app.services.bridge_client import BridgeClient
from apps.api.app.services.redis_client import close_redis, get_redis
from apps.api.app.settings import get_settings


# --------------------------------------------------------------------------
# pure wiring
# --------------------------------------------------------------------------
def test_bridge_channels_are_connectable_and_routed() -> None:
    for ct in ("whatsapp_app", "line_app"):
        assert ct in ch_router._CONNECTABLE
        assert ct in ch_router._BRIDGE_CHANNELS
    assert dsvc.BRIDGE_CHANNELS == {"whatsapp_app": "wa_app", "line_app": "line_app"}


def test_bridge_adapters_registered() -> None:
    assert get_adapter("whatsapp_app").channel_type == "whatsapp_app"
    assert get_adapter("line_app").channel_type == "line_app"


def test_status_mapping_mirrors_ingress() -> None:
    assert dsvc._acct_status("online") == "active"
    assert dsvc._acct_status("offline") == "disconnected"
    assert dsvc._acct_status("awaiting_qr") == "awaiting_qr"
    assert dsvc._acct_status("banned") == "banned"


# --------------------------------------------------------------------------
# live provisioning (mocked bridge)
# --------------------------------------------------------------------------
class FakeBridge:
    def __init__(self, health: dict | None = None):
        self.created: dict | None = None
        self.logged_out = False
        self._health = health or {"status": "online", "phone": "+5511999", "pushname": "Joe"}

    async def create_device(self, device_id, *, callback_url, callback_secret):
        self.created = {
            "device_id": device_id,
            "callback_url": callback_url,
            "callback_secret": callback_secret,
        }
        return {"device_id": device_id, "status": "awaiting_qr"}

    async def get_qr(self, device_id):
        return {"qr": "2@qr-payload", "status": "awaiting_qr"}

    async def get_health(self, device_id):
        return self._health

    async def logout(self, device_id):
        self.logged_out = True
        return {"ok": True}

    async def delete_device(self, device_id):
        return {"ok": True}


def _rebind_infra() -> None:
    """Reset loop-bound singletons so this test binds pg+redis to its own loop."""
    import apps.api.app.services.redis_client as rc

    dbmod._engine = None
    dbmod._session_factory = None
    rc._redis = None


async def _infra_up() -> bool:
    try:
        async with dbmod.session_factory()() as s:
            await s.execute(sa.text("SELECT 1"))
        await get_redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _member(ws: Workspace) -> MemberContext:
    return MemberContext(
        member=SimpleNamespace(id=uuid.uuid4()),
        workspace=ws,
        user=SimpleNamespace(id=uuid.uuid4()),
        permissions={"*"},
    )


async def test_provision_and_lifecycle() -> None:
    _rebind_infra()
    if not await _infra_up():
        pytest.skip("pg/redis not available")

    settings = get_settings()
    old_url = settings.bridge_wa_url
    settings.bridge_wa_url = "http://bridge-wa:8100"
    sf = dbmod.session_factory()
    touched: list[uuid.UUID] = []
    try:
        async with sf() as session:
            ws = Workspace(name="wa-app-test", plan_code="free", status="active", settings={})
            session.add(ws)
            await session.flush()
            touched.append(ws.id)
            member = _member(ws)

            # ---- happy path: bridge online, QR login started ----
            fake = FakeBridge()
            out = await dsvc.provision_device(
                session, member, "whatsapp_app", name="My WA", client=fake
            )
            acct_id = uuid.UUID(out["id"])
            assert out["status"] == "awaiting_qr"
            assert out["device_id"] == str(acct_id)
            assert "error" not in out
            # bridge got the device with the right callback + secret
            assert fake.created["device_id"] == str(acct_id)
            assert fake.created["callback_url"].endswith(f"/hooks/bridge/{fake.created['callback_secret']}")

            acct = await session.get(ChannelAccount, acct_id)
            assert acct.channel_type == "whatsapp_app"
            assert acct.status == "awaiting_qr"
            assert acct.config["bridge_type"] == "wa_app"
            assert acct.config["bridge_url"] == f"http://bridge-wa:8100/devices/{acct_id}"
            secret = acct.config["webhook_secret"]
            assert acct.webhook_secret == secret
            # serialized config must NOT leak the secret
            assert "webhook_secret" not in out["config"]

            bridge = (
                await session.execute(
                    sa.select(DeviceBridge).where(DeviceBridge.channel_account_id == acct_id)
                )
            ).scalar_one()
            assert bridge.bridge_type == "wa_app"
            assert bridge.status == "awaiting_qr"
            assert bridge.config["device_id"] == str(acct_id)

            # credential is the HMAC signing key used by the outbound BridgeAdapter
            creds = await get_credentials(session, acct)
            assert creds == {"bridge_token": secret}

            # ---- get_qr proxies the bridge ----
            qr = await dsvc.get_qr(session, member, "whatsapp_app", acct_id, client=fake)
            assert qr == {"qr": "2@qr-payload", "status": "awaiting_qr"}

            # ---- refresh_status: online → active, pause key cleared ----
            await get_redis().set(pause_key(acct_id), "offline", ex=60)
            st = await dsvc.refresh_status(session, member, "whatsapp_app", acct_id, client=fake)
            assert st["status"] == "active"
            assert st["bridge_status"] == "online"
            assert st["profile"]["phone"] == "+5511999"
            assert await get_redis().get(pause_key(acct_id)) is None  # resumed
            acct = await session.get(ChannelAccount, acct_id)
            assert acct.status == "active"
            assert acct.health["last_status"] == "online"

            # ---- refresh_status: offline → disconnected, pause key set ----
            st2 = await dsvc.refresh_status(
                session, member, "whatsapp_app", acct_id,
                client=FakeBridge(health={"status": "offline"}),
            )
            assert st2["status"] == "disconnected"
            assert (await get_redis().get(pause_key(acct_id))) is not None  # paused

            # ---- logout is terminal ----
            lo = await dsvc.logout(session, member, "whatsapp_app", acct_id, client=fake)
            assert lo == {"ok": True, "status": "logged_out"} or lo.get("status") == "logged_out"
            assert fake.logged_out is True
            acct = await session.get(ChannelAccount, acct_id)
            assert acct.status == "logged_out"

            # ---- degraded: bridge offline/unconfigured → pending + surfaced error ----
            disabled = BridgeClient("", "tok")  # no URL
            out2 = await dsvc.provision_device(
                session, member, "whatsapp_app", name="Offline WA", client=disabled
            )
            assert out2["status"] == "pending"
            assert out2["bridge_disabled"] is True
            assert "error" in out2
            acct2 = await session.get(ChannelAccount, uuid.UUID(out2["id"]))
            assert acct2 is not None and acct2.status == "pending"  # still created
            bridge2 = (
                await session.execute(
                    sa.select(DeviceBridge).where(DeviceBridge.channel_account_id == acct2.id)
                )
            ).scalar_one()
            assert bridge2.status == "offline"

            print("device provisioning lifecycle: PASS")
    finally:
        settings.bridge_wa_url = old_url
        async with sf() as session:
            for wid in touched:
                ws = await session.get(Workspace, wid)
                if ws is not None:
                    await session.delete(ws)  # cascade → accounts + bridges
            await session.commit()
        await close_redis()
        # dispose the loop-bound asyncpg engine so a later live test (on its own
        # event loop) recreates a fresh one instead of skipping.
        await dbmod.engine().dispose()
        dbmod._engine = None
        dbmod._session_factory = None
