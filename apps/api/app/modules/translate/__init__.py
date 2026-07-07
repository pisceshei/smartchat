"""Translation + composer-assist execution API (/api/v1/ai).

The P1 inbox owns the per-conversation translation toggle STATE
(/api/v1/inbox/conversations/{id}/translation); this module adds the actual
translate execution (on-demand + inbound message) and the SSE composer assist.
"""
from __future__ import annotations
