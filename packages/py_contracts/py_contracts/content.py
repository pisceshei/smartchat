"""Canonical message content model — the single discriminated union used by
channel adapters (normalize once), the messages table (stored as-is), the
widget protocol, quick replies, and flow send_message nodes.

Extending: add a new block type here first; adapters degrade per their
capability matrix, never per ad-hoc checks.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    kind: Literal["text"] = "text"
    text: str


class MediaBlock(BaseModel):
    kind: Literal["media"] = "media"
    media_type: Literal["image", "video", "audio", "voice", "file", "sticker"]
    file_id: UUID
    caption: str | None = None
    mime: str | None = None
    size: int | None = None
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None


class CardButton(BaseModel):
    text: str
    action: Literal["url", "postback"]
    value: str  # url or postback payload


class ProductCardBlock(BaseModel):
    kind: Literal["product_card"] = "product_card"
    title: str
    subtitle: str | None = None
    image_file_id: UUID | None = None
    image_url: str | None = None
    price: str | None = None
    currency: str | None = None
    url: str | None = None
    buttons: list[CardButton] = Field(default_factory=list)


class QuickButton(BaseModel):
    id: str
    text: str


class QuickButtonsBlock(BaseModel):
    kind: Literal["quick_buttons"] = "quick_buttons"
    text: str
    buttons: list[QuickButton]


class ButtonReplyBlock(BaseModel):
    """Inbound: customer tapped a quick button."""

    kind: Literal["button_reply"] = "button_reply"
    payload: str
    text: str
    flow_session_id: UUID | None = None


class TemplateBlock(BaseModel):
    """WhatsApp Cloud API template send."""

    kind: Literal["template"] = "template"
    template_name: str
    language: str
    components: dict = Field(default_factory=dict)
    category: str | None = None


class LocationBlock(BaseModel):
    kind: Literal["location"] = "location"
    latitude: float
    longitude: float
    name: str | None = None
    address: str | None = None


class EmailBlock(BaseModel):
    kind: Literal["email"] = "email"
    subject: str | None = None
    text: str
    html_body_file_id: UUID | None = None
    headers: dict = Field(default_factory=dict)  # message_id / in_reply_to / references[]
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)


class SystemEventBlock(BaseModel):
    """Grey timeline chips: assigned / closed / member joined / etc."""

    kind: Literal["system_event"] = "system_event"
    event: str
    meta: dict = Field(default_factory=dict)


ContentBlock = Annotated[
    Union[
        TextBlock,
        MediaBlock,
        ProductCardBlock,
        QuickButtonsBlock,
        ButtonReplyBlock,
        TemplateBlock,
        LocationBlock,
        EmailBlock,
        SystemEventBlock,
    ],
    Field(discriminator="kind"),
]


class MessageContent(BaseModel):
    """A message = ordered list of blocks. Most are single-block."""

    blocks: list[ContentBlock]

    def plain_text(self) -> str:
        parts: list[str] = []
        for b in self.blocks:
            match b:
                case TextBlock():
                    parts.append(b.text)
                case MediaBlock() if b.caption:
                    parts.append(b.caption)
                case ProductCardBlock():
                    parts.append(b.title)
                case QuickButtonsBlock():
                    parts.append(b.text)
                case ButtonReplyBlock():
                    parts.append(b.text)
                case EmailBlock():
                    parts.append(b.subject or b.text[:200])
                case _:
                    pass
        return "\n".join(p for p in parts if p)
