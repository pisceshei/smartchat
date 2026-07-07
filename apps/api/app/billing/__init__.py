"""Billing domain logic (P3 計費模型實測).

Pure-ish, session-based helpers reused by the ``modules.billing`` HTTP surface,
the Stripe webhook, and the admin self-use path:

- ``balance``      — prepaid 餘額 read / top-up / order-apply / refund (ledgered).
- ``subscription`` — plan+duration change effects (extend current_period_end,
  add-on quota expansion via plan_overrides) + expiry sweep.
- ``points_topup`` — grant purchased AI points (idempotent by order ref).
- ``gating``       — feature-gate matrix + ``require_feature`` re-export.

Order math itself lives in ``services.stripe_client.compute_order`` (pure).
"""
from __future__ import annotations
