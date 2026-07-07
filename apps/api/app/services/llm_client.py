"""LLM client wiring (plan B.0 / B.2).

Business code asks for a *tier* (fast/smart/embed); the active LLMProfile maps
tiers to models and points at any Anthropic- or OpenAI-compatible endpoint (the
sub2api relay). Swapping providers is a config edit — zero business code change.

- ``build_profile()``   — Settings → py_contracts.llm.LLMProfile
- ``profile_from_row()``— tenancy.LLMProfileRow (+ decrypted key) → LLMProfile
- ``get_llm_client()``  — LLMProfile → LLMClient
- ``get_default_llm()`` — process-wide singleton built from Settings
- ``set_default_llm()`` / ``reset_default_llm()`` — dependency injection (tests
  pass a FakeLLM implementing LLMClientProtocol)
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from py_contracts.llm import LLMClient, LLMMessage, LLMProfile, Tier

from ..settings import Settings, get_settings

if TYPE_CHECKING:
    from ..models.tenancy import LLMProfileRow


@runtime_checkable
class LLMClientProtocol(Protocol):
    """The injectable boundary. The real py_contracts.llm.LLMClient satisfies
    this; tests supply a FakeLLM with the same surface (never hitting a real
    endpoint)."""

    async def complete(
        self,
        *,
        tier: Tier,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = ...,
        temperature: float = ...,
    ) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


def build_profile(settings: Settings | None = None) -> LLMProfile:
    """Assemble the default LLMProfile from Settings (env-driven)."""
    s = settings or get_settings()
    provider = "anthropic" if s.llm_provider == "anthropic" else "openai_compat"
    model_map: dict[Tier, str] = {
        "fast": s.llm_model_fast,
        "smart": s.llm_model_smart,
        "embed": s.llm_model_embed,
    }
    return LLMProfile(
        provider=provider,  # type: ignore[arg-type]
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model_map=model_map,
    )


def profile_from_row(row: LLMProfileRow, api_key: str) -> LLMProfile:
    """Build a profile from a per-workspace override row (plan B.0). The caller
    passes the decrypted api_key (envelope-decryption lives in the crypto
    service, out of this module's scope)."""
    provider = "anthropic" if row.provider == "anthropic" else "openai_compat"
    return LLMProfile(
        provider=provider,  # type: ignore[arg-type]
        base_url=row.base_url,
        api_key=api_key,
        model_map=dict(row.model_map or {}),  # type: ignore[arg-type]
        timeout_s=float(row.timeout_s or 60),
        max_concurrency=int(row.max_concurrency or 8),
    )


def get_llm_client(profile: LLMProfile) -> LLMClient:
    """Instantiate a client for an explicit profile (e.g. a per-workspace
    override). Caller owns its lifecycle (call ``aclose()``)."""
    return LLMClient(profile)


# --------------------------------------------------------------------------
# process-wide default singleton (dependency-injectable)
# --------------------------------------------------------------------------
_default: LLMClientProtocol | None = None


def get_default_llm() -> LLMClientProtocol:
    """The shared default client built from Settings. Lazily constructed;
    overridable via set_default_llm (tests inject a FakeLLM)."""
    global _default
    if _default is None:
        _default = LLMClient(build_profile())
    return _default


def set_default_llm(client: LLMClientProtocol | None) -> None:
    """Inject a client (e.g. a FakeLLM in tests, or a pre-warmed singleton at
    app startup). Passing None clears it so the next get rebuilds from Settings."""
    global _default
    _default = client


def reset_default_llm() -> None:
    set_default_llm(None)
