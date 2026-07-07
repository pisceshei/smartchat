"""Relay-swappable LLM client + marker protocol.

Business code requests a *tier* ("fast" | "smart" | "embed"), never a model
name. The active profile maps tiers to models and points at any
Anthropic-compatible or OpenAI-compatible endpoint (sub2api relay, official
API, or a future替换) — swapping providers is a config row edit, zero code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal

import httpx

Tier = Literal["fast", "smart", "embed"]


@dataclass
class LLMProfile:
    provider: Literal["anthropic", "openai_compat"]
    base_url: str  # e.g. sub2api relay root
    api_key: str
    model_map: dict[Tier, str] = field(default_factory=dict)
    timeout_s: float = 60.0
    max_concurrency: int = 8


@dataclass
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


class LLMClient:
    def __init__(self, profile: LLMProfile):
        self.profile = profile
        self._client = httpx.AsyncClient(base_url=profile.base_url, timeout=profile.timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> str:
        model = self.profile.model_map[tier]
        if self.profile.provider == "anthropic":
            resp = await self._client.post(
                "/v1/messages",
                headers={
                    "x-api-key": self.profile.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": model,
                    "system": system,
                    "messages": [m.__dict__ for m in messages],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return "".join(b.get("text", "") for b in data.get("content", []))
        # openai_compat
        resp = await self._client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.profile.api_key}"},
            json={
                "model": model,
                "messages": [{"role": "system", "content": system}]
                + [m.__dict__ for m in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        model = self.profile.model_map["embed"]
        resp = await self._client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {self.profile.api_key}"},
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]


# ---------------------------------------------------------------------------
# Marker protocol — survives any relay because it's plain text, no tool-calling.
# [CARD:handle1,handle2]  → product cards (validated against catalog upstream)
# [HANDOFF:reason]        → escalate to human
# [LEAD:field=value]      → write a contact field
# ---------------------------------------------------------------------------
_CARD_RE = re.compile(r"\[CARD:([^\]]+)\]")
_HANDOFF_RE = re.compile(r"\[HANDOFF:([^\]]*)\]")
_LEAD_RE = re.compile(r"\[LEAD:([a-zA-Z0-9_.]+)=([^\]]*)\]")


@dataclass
class ParsedReply:
    text: str
    card_handles: list[str] = field(default_factory=list)
    handoff_reason: str | None = None
    lead_fields: dict[str, str] = field(default_factory=dict)


def parse_markers(raw: str) -> ParsedReply:
    handles: list[str] = []
    for m in _CARD_RE.finditer(raw):
        handles.extend(h.strip() for h in m.group(1).split(",") if h.strip())
    handoff = None
    hm = _HANDOFF_RE.search(raw)
    if hm:
        handoff = hm.group(1).strip() or "unspecified"
    leads = {m.group(1): m.group(2).strip() for m in _LEAD_RE.finditer(raw)}
    text = _CARD_RE.sub("", raw)
    text = _HANDOFF_RE.sub("", text)
    text = _LEAD_RE.sub("", text)
    return ParsedReply(
        text=re.sub(r"\n{3,}", "\n\n", text).strip(),
        card_handles=handles,
        handoff_reason=handoff,
        lead_fields=leads,
    )
