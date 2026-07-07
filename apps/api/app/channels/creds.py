"""Channel-account credential helpers (envelope encryption, plan A.0/A.3).

Credentials live in channel_accounts.credentials_enc as a Fernet blob
encrypted with the per-workspace data key (itself wrapped by
CREDENTIALS_MASTER_KEY). Never store credentials in JSONB.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.channels import ChannelAccount
from ..models.tenancy import Workspace
from ..services.security import get_cipher


async def _data_key(session: AsyncSession, workspace_id: Any) -> bytes:
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise ValueError(f"workspace {workspace_id} not found")
    if ws.data_key_enc is None:
        ws.data_key_enc = get_cipher().new_wrapped_data_key()
        await session.flush()
    return bytes(ws.data_key_enc)


async def get_credentials(session: AsyncSession, account: ChannelAccount) -> dict[str, Any]:
    if account.credentials_enc is None:
        return {}
    key = await _data_key(session, account.workspace_id)
    creds = get_cipher().decrypt_json(key, bytes(account.credentials_enc))
    return creds if isinstance(creds, dict) else {}


async def set_credentials(
    session: AsyncSession, account: ChannelAccount, credentials: dict[str, Any]
) -> None:
    key = await _data_key(session, account.workspace_id)
    account.credentials_enc = get_cipher().encrypt_json(key, credentials)
