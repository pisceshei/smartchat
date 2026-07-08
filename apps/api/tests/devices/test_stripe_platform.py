"""Platform Stripe config: DB-first key resolution + encrypted-at-rest roundtrip.

The pure tests exercise the process-cache precedence (DB key wins over env,
invalidation reverts) without a DB or the Stripe SDK. The live test (auto-skips
if pg is down) proves set → load → get is encrypted at rest and DB-first, then
cleans up the singleton row so it never leaks into other tests.
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa

import apps.api.app.db as dbmod
import apps.api.app.services.stripe_client as sc
from apps.api.app.settings import Settings


def _settings(**kw) -> Settings:
    base = {
        "stripe_secret_key": "sk_env",
        "stripe_webhook_secret": "whsec_env",
        "stripe_publishable_key": "pk_env",
    }
    base.update(kw)
    return Settings(**base)


def test_effective_key_prefers_cache_over_env() -> None:
    sc.invalidate_platform_stripe()
    s = _settings()
    # cold cache → env
    assert sc._effective_secret_key(s) == "sk_env"
    assert sc._effective_webhook_secret(s) == "whsec_env"
    # warm cache with a DB override → DB wins
    sc._platform_cache_set(
        {"secret_key": "sk_db", "webhook_secret": "whsec_db", "publishable_key": "pk_db", "currency": ""}
    )
    try:
        assert sc._effective_secret_key(s) == "sk_db"
        assert sc._effective_webhook_secret(s) == "whsec_db"
    finally:
        sc.invalidate_platform_stripe()
    # invalidated → back to env
    assert sc._effective_secret_key(s) == "sk_env"


def test_empty_cache_value_falls_through_to_env() -> None:
    sc.invalidate_platform_stripe()
    s = _settings()
    sc._platform_cache_set({"secret_key": "", "webhook_secret": "", "publishable_key": "", "currency": ""})
    try:
        assert sc._effective_secret_key(s) == "sk_env"
        assert sc._effective_webhook_secret(s) == "whsec_env"
    finally:
        sc.invalidate_platform_stripe()


def test_get_stripe_none_without_any_key() -> None:
    sc.invalidate_platform_stripe()
    s = _settings(stripe_secret_key="")
    # no env key, empty cache → disabled regardless of SDK presence
    assert sc.get_stripe(s) is None


def test_construct_event_requires_webhook_secret() -> None:
    sc.invalidate_platform_stripe()
    s = _settings(stripe_webhook_secret="")
    with pytest.raises(sc.BillingDisabledError):
        sc.construct_event(b"{}", "sig", s)


def _rebind_infra() -> None:
    """Reset the loop-bound asyncpg engine so this test recreates one on its own
    event loop (another live test may have bound the global to a prior loop)."""
    dbmod._engine = None
    dbmod._session_factory = None


async def _db_up() -> bool:
    try:
        async with dbmod.session_factory()() as s:
            await s.execute(sa.text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def test_platform_stripe_roundtrip_encrypted_and_db_first() -> None:
    _rebind_infra()
    if not await _db_up():
        pytest.skip("pg not available")
    from apps.api.app.models.platform import PLATFORM_SETTINGS_ID, PlatformSettings

    sf = dbmod.session_factory()
    sc.invalidate_platform_stripe()
    try:
        async with sf() as session:
            await sc.set_platform_stripe(
                session,
                secret_key="sk_test_db_123",
                publishable_key="pk_test_db",
                webhook_secret="whsec_db_456",
                currency="eur",
            )
            await session.commit()

        # encrypted at rest: the ciphertext must not contain the plaintext
        async with sf() as session:
            row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
            assert row is not None
            assert row.stripe_secret_enc is not None
            assert b"sk_test_db_123" not in bytes(row.stripe_secret_enc)
            assert row.stripe_publishable == "pk_test_db"  # public → plaintext ok
            assert row.stripe_currency == "eur"

            # load decrypts + warms the cache
            cfg = await sc.load_platform_stripe(session)
            assert cfg["secret_key"] == "sk_test_db_123"
            assert cfg["webhook_secret"] == "whsec_db_456"
            assert cfg["publishable_key"] == "pk_test_db"

        # DB-first: with the cache warm the effective key is the DB one
        s = _settings()  # env has sk_env, but DB override must win
        assert sc._effective_secret_key(s) == "sk_test_db_123"
        assert sc._effective_webhook_secret(s) == "whsec_db_456"

        async with sf() as session:
            status = await sc.get_platform_stripe_status(session, s)
            assert status["secret_key_set"] is True
            assert status["secret_source"] == "db"
            assert status["publishable_key"] == "pk_test_db"
            assert status["webhook_source"] == "db"
            assert "sk_test_db_123" not in str(status)  # never leaks the secret

        # clearing a value reverts to env
        async with sf() as session:
            await sc.set_platform_stripe(session, secret_key="")
            await session.commit()
            cfg2 = await sc.load_platform_stripe(session)
            assert cfg2["secret_key"] == ""
        assert sc._effective_secret_key(s) == "sk_env"
    finally:
        # remove the singleton row so the override never leaks into other tests
        async with sf() as session:
            await session.execute(sa.text("DELETE FROM platform_settings"))
            await session.commit()
        sc.invalidate_platform_stripe()
        # dispose the loop-bound asyncpg engine so a later live test (on its own
        # event loop) recreates a fresh one instead of skipping.
        await dbmod.engine().dispose()
        dbmod._engine = None
        dbmod._session_factory = None
