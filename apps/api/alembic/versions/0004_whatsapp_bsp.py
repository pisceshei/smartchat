"""P4: add whatsapp_bsp to the channel_accounts.channel_type CHECK constraint.

The BSP-proxy WhatsApp adapter (YCloud/ChatApp/…) persists ChannelAccount rows
with channel_type='whatsapp_bsp'; the original 0001 CHECK did not list it.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_channel_accounts_type"
_OLD = (
    "channel_type IN ('widget','telegram_bot','telegram_app','email','messenger',"
    "'instagram','whatsapp_cloud','whatsapp_app','line_oa','line_app','wechat',"
    "'wecom','wechat_kf','tiktok','tiktok_business','youtube','zalo_app','slack',"
    "'vk','sms')"
)
_NEW = (
    "channel_type IN ('widget','telegram_bot','telegram_app','email','messenger',"
    "'instagram','whatsapp_cloud','whatsapp_app','whatsapp_bsp','line_oa','line_app',"
    "'wechat','wecom','wechat_kf','tiktok','tiktok_business','youtube','zalo_app',"
    "'slack','vk','sms')"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "channel_accounts", type_="check")
    op.create_check_constraint(_CONSTRAINT, "channel_accounts", _NEW)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "channel_accounts", type_="check")
    op.create_check_constraint(_CONSTRAINT, "channel_accounts", _OLD)
