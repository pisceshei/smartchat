"""Channel adapter layer (plan 附錄 A.7).

- base.py             ChannelAdapter protocol, InboundEvent union, Capabilities matrix
- registry.py         channel_type → adapter singleton
- ratelimit.py        per-account Redis token buckets (pure math + Lua)
- media.py            MinIO-backed media store (inbound media is copied at ingest)
- creds.py            envelope-encrypted credential helpers
- ingress_pipeline.py webhook raw stream → normalized, deduped, persisted messages
- sender.py           transactional-outbox consumer (render → throttle → send → retry)
- adapters/           one module per channel
"""
