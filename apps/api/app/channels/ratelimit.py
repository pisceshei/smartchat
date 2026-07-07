"""Per-account Redis token buckets (plan A.7 outbound rates).

Rates: wa_cloud 80/s · messenger 250/s · telegram 30/s global + 1/s per chat ·
line 2000/min · smtp 10/min · wa_app humanized 6–10/min with jitter.

The bucket math is a pure function (unit-tested) mirrored 1:1 by the Lua
script that runs atomically in Redis.
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis


@dataclass(frozen=True)
class RateSpec:
    rate_per_s: float          # steady-state refill rate
    burst: float               # bucket capacity
    per_chat_rate_per_s: float | None = None
    per_chat_burst: float = 1.0
    humanize_per_min: tuple[float, float] | None = None  # (min, max) msgs/min jitter


RATES: dict[str, RateSpec] = {
    "whatsapp_cloud": RateSpec(rate_per_s=80.0, burst=80.0),
    "messenger": RateSpec(rate_per_s=250.0, burst=250.0),
    "instagram": RateSpec(rate_per_s=250.0, burst=250.0),
    "telegram_bot": RateSpec(rate_per_s=30.0, burst=30.0, per_chat_rate_per_s=1.0, per_chat_burst=1.0),
    "line_oa": RateSpec(rate_per_s=2000.0 / 60.0, burst=100.0),
    "email": RateSpec(rate_per_s=10.0 / 60.0, burst=2.0),
    "whatsapp_app": RateSpec(rate_per_s=8.0 / 60.0, burst=1.0, humanize_per_min=(6.0, 10.0)),
    "line_app": RateSpec(rate_per_s=8.0 / 60.0, burst=1.0, humanize_per_min=(6.0, 10.0)),
    "widget": RateSpec(rate_per_s=200.0, burst=200.0),
}

DEFAULT_SPEC = RateSpec(rate_per_s=5.0, burst=10.0)


def spec_for(channel_type: str) -> RateSpec:
    return RATES.get(channel_type, DEFAULT_SPEC)


def effective_rate(spec: RateSpec) -> float:
    """Humanized channels draw a jittered rate per acquisition (仿人 6–10/min)."""
    if spec.humanize_per_min:
        lo, hi = spec.humanize_per_min
        return random.uniform(lo, hi) / 60.0
    return spec.rate_per_s


# --------------------------------------------------------------------------
# pure bucket math (mirrored by the Lua script)
# --------------------------------------------------------------------------
def refill_tokens(tokens: float, updated_at: float, now: float, rate: float, burst: float) -> float:
    if now <= updated_at:
        return min(tokens, burst)
    return min(burst, tokens + (now - updated_at) * rate)


def try_take(
    tokens: float,
    updated_at: float,
    now: float,
    rate: float,
    burst: float,
    n: float = 1.0,
) -> tuple[bool, float, float]:
    """Returns (allowed, remaining_tokens, wait_seconds)."""
    available = refill_tokens(tokens, updated_at, now, rate, burst)
    if available >= n:
        return True, available - n, 0.0
    wait = (n - available) / rate if rate > 0 else 60.0
    return False, available, wait


_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = burst end
if ts == nil then ts = now end
if now > ts then
  tokens = math.min(burst, tokens + (now - ts) * rate)
else
  tokens = math.min(tokens, burst)
end
local allowed = 0
local wait = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
else
  if rate > 0 then wait = (requested - tokens) / rate else wait = 60 end
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
local ttl = 60
if rate > 0 then ttl = math.ceil(burst / rate) + 60 end
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(wait)}
"""


class RateLimitTimeout(Exception):
    """Could not obtain a send slot within max_wait (job should back off)."""


def account_bucket_key(channel_type: str, account_id: uuid.UUID | str) -> str:
    return f"rate:acct:{channel_type}:{account_id}"


def chat_bucket_key(channel_type: str, account_id: uuid.UUID | str, chat_id: str) -> str:
    return f"rate:chat:{channel_type}:{account_id}:{chat_id}"


async def acquire(
    redis: aioredis.Redis,
    key: str,
    *,
    rate: float,
    burst: float,
    n: float = 1.0,
    now: float | None = None,
) -> float:
    """Atomically take n tokens. Returns 0.0 on success, else the seconds to
    wait before tokens will be available."""
    now = now if now is not None else time.time()
    res: Any = await redis.eval(_TOKEN_BUCKET_LUA, 1, key, str(rate), str(burst), str(now), str(n))
    allowed = int(res[0])
    wait = float(res[1])
    return 0.0 if allowed == 1 else max(wait, 0.001)


async def wait_for_slot(
    redis: aioredis.Redis,
    *,
    channel_type: str,
    account_id: uuid.UUID | str,
    chat_id: str | None = None,
    max_wait: float = 30.0,
) -> None:
    """Block (async) until both the per-account and (if applicable) per-chat
    buckets grant a token, or raise RateLimitTimeout after max_wait."""
    spec = spec_for(channel_type)
    deadline = time.monotonic() + max_wait
    while True:
        rate = effective_rate(spec)
        wait = await acquire(
            redis, account_bucket_key(channel_type, account_id), rate=rate, burst=spec.burst
        )
        if wait == 0.0 and spec.per_chat_rate_per_s is not None and chat_id:
            wait = await acquire(
                redis,
                chat_bucket_key(channel_type, account_id, chat_id),
                rate=spec.per_chat_rate_per_s,
                burst=spec.per_chat_burst,
            )
        if wait == 0.0:
            return
        if time.monotonic() + wait > deadline:
            raise RateLimitTimeout(
                f"no send slot for {channel_type}:{account_id} within {max_wait}s"
            )
        await asyncio.sleep(min(wait, 5.0))
