"""Broadcast / marketing machinery (plan 附錄 B.3).

Split across small modules so the pure logic is trivially unit-testable:

- ``schedule``       recurrence (rrule subset) + send-window math (pure)
- ``split_strategy`` split-link target selection: random / time_period /
                     sequential, with weights + time-windows + daily caps (pure)
- ``suppression``    unsubscribe / blacklist / per-week frequency cap / dedupe
- ``recipients``     identity resolution + the recipient state machine + the
                     delivery-status → recipient bridge + run-counter roll-up
- ``fanout``         the ARQ tasks: run materialisation → suppression → 500-chunk
                     send via ``messaging.send_message(sender_type='campaign')``,
                     the recurring scheduler tick, the recycle-bin purge, the
                     delivery-status drain, and the WhatsApp approval reconcile
- ``wa_template_sync`` Meta Graph message_templates approval sync
- ``attribution``    inbound tracking-code → conversation attribution helper the
                     channel ingress can call (does not rewrite ingress)
"""
from __future__ import annotations
