"""Test doubles for the AI subsystem — never touch a network or the DB.

FakeLLM implements the LLMClientProtocol (deterministic completions +
hash-derived embeddings so cosine distance is meaningful). FakeRedis is a tiny
in-memory async subset. FakeSession/FakeResult stub the SQLAlchemy surface the
pure-ish AI helpers touch.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

EMBED_DIM = 1024


class FakeLLM:
    """Injectable LLM stand-in. `complete` is routed by system-prompt keyword so
    one instance serves condense / reply / summary / translate / intent."""

    def __init__(self, *, reply: str = "OK", intent_choice: str = "0", dim: int = EMBED_DIM):
        self.reply = reply
        self.intent_choice = intent_choice
        self.dim = dim
        self.complete_calls = 0
        self.embed_calls: list[list[str]] = []
        self.systems: list[str] = []

    async def complete(self, *, tier, system, messages, max_tokens=1024, temperature=0.3) -> str:
        self.complete_calls += 1
        self.systems.append(system)
        s = system or ""
        if "standalone search query" in s:
            return messages[-1].content if messages else ""
        if "intent classifier" in s:
            return self.intent_choice
        if "Summarise" in s or "summary" in s.lower():
            return "Customer needs help; unresolved."
        if "translator" in s.lower():
            # echo a numbered translation for each input line
            lines = (messages[-1].content if messages else "").splitlines()
            return "\n".join(f"{i + 1}. [t]{ln.split('. ', 1)[-1]}" for i, ln in enumerate(lines))
        return self.reply

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        """Deterministic unit-ish vector seeded from the text hash so distinct
        texts get distinct directions (constant vectors would tie on cosine)."""
        h = hashlib.sha256((text or "").encode("utf-8")).digest()
        vals: list[float] = []
        i = 0
        while len(vals) < self.dim:
            vals.append((h[i % len(h)] / 255.0) - 0.5 + (i * 0.0001))
            i += 1
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        return [v / norm for v in vals]

    async def aclose(self) -> None:
        pass


class FakeRedis:
    """In-memory subset of redis.asyncio used by cache/idempotency paths."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: Any, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        return True

    async def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            n += 1 if self.store.pop(k, None) is not None else 0
        return n

    async def incr(self, key: str) -> int:
        v = int(self.store.get(key, "0")) + 1
        self.store[key] = str(v)
        return v

    async def expire(self, key: str, ttl: int) -> bool:
        return key in self.store


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = list(rows)

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Configurable async-session stub. `execute` returns a preset FakeResult
    (or one built by a callable); `get` is served by a callable; `add`/`flush`
    are recorded no-ops."""

    def __init__(self, *, execute: Any = None, get: Any = None):
        self._execute = execute
        self._get = get
        self.added: list[Any] = []

    async def execute(self, stmt: Any, *a: Any, **k: Any) -> Any:
        if callable(self._execute):
            return self._execute(stmt)
        return self._execute if self._execute is not None else FakeResult([])

    async def get(self, model: Any, key: Any) -> Any:
        if callable(self._get):
            return self._get(model, key)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None
