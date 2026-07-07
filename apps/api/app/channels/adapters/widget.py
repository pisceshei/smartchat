"""Website chat widget — the internal channel.

Inbound: the widget REST module normalizes visitor input itself and enqueues
pre-normalized events ({"events": [...]}) on ingress:widget, so parse_inbound
is the shared normalized-events parser.

Outbound: `send` is a no-op write-through — persistence already happened in
the outbox and delivery to the browser is the realtime layer's job (the
ws-gateway fans out the message.created / message.updated events this
pipeline emits). The widget supports the full rich block set, so render is
the identity mapping.
"""
from __future__ import annotations

import uuid
from typing import Any, ClassVar

from py_contracts.content import MessageContent

from ..base import BaseAdapter, HealthResult, SendResult, degrade_content


class WidgetAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "widget"

    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities)
        return [{"blocks": degraded.model_dump(mode="json")["blocks"]}]

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        # Delivery = realtime fan-out of the message.created event; nothing to
        # call. A synthetic external id keeps delivery-status plumbing uniform.
        return SendResult(ok=True, external_message_id=f"widget:{uuid.uuid4()}")

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        return HealthResult(ok=True, status="active")
