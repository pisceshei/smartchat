# SmartChat — agent orientation (read this first)

SmartChat is a **from-scratch, multi-tenant SaaS clone of SaleSmartly**: an
omnichannel customer-service + marketing platform (inbox, 17 channel adapters,
visitor chat widget, automation flow engine, AI agents with RAG, broadcasts,
reports, Stripe billing). It is **live in production** at
`https://chat.chilling.com.hk`. This file orients a fresh agent; the living
state (what's deployed, known bugs, roadmap, every gotcha) is in
**[docs/PROJECT_STATE.md](docs/PROJECT_STATE.md)** — read it before touching prod.

## Golden rules (learned the hard way — see PROJECT_STATE for the incident behind each)
1. **Every `docker compose` command on the server needs `--env-file .env`.**
   `-f infra/docker-compose.yml` makes compose look for `infra/.env` (absent) for
   `${POSTGRES_PASSWORD}`/`${MINIO_ROOT_PASSWORD}` interpolation and silently
   falls back to defaults → auth failures. Alias it: `dc='docker compose -f infra/docker-compose.yml --env-file .env'`.
2. **Secrets never go in git.** `.env`, `infra/.env.prod.example` (has a real LLM
   key) are gitignored. The classifier will (correctly) block force-adding them.
   Server `.env` is written by hand; `CREDENTIALS_MASTER_KEY` must stay stable
   forever or all encrypted channel credentials break.
3. **Each backend service has its own image** (`smartchat-api`, `-worker`,
   `-ai-agent`, `-ws-gateway`, `-beat`, `-flow-engine`, `-edge`). Rebuilding
   `api` does NOT rebuild the others — after a backend change rebuild them all:
   `dc build api ws-gateway worker beat flow-engine channel-ingress ai-agent edge`.
4. **Frontend `dist/` is gitignored** — built inside Docker (widget in the api
   image's node stage; admin SPA in the `web` nginx image). Rebuild `web` after
   any `apps/web` change, `api` after any `apps/widget` change.
5. **Don't enter passwords/credentials into forms or create accounts** for the
   user — deployment provisioning (server-side scripts) is fine, interactive
   credential entry is not. Stripe/Telegram/channel tokens are entered by the
   user in the admin backend after deploy.

## Tech stack
- **Backend** `apps/api` — Python 3.12 · FastAPI (async) · SQLAlchemy 2 ·
  PostgreSQL 16 + pgvector · Redis 7 (Streams event bus + timers) · ARQ jobs ·
  MinIO (S3). One image, many entrypoints (see compose `command:`).
- **Admin SPA** `apps/web` — React 18 · TS · Vite · AntD 5 · Zustand · react-query · @xyflow/react. Calls API same-origin (`/api/v1`, `/ws/agent`).
- **Visitor widget** `apps/widget` — Preact · loader (vanilla, <25KB) + iframe chat app. Served by the api at `/js/project_{key}.js` + `/widget-app/`.
- **WhatsApp-App bridge** `apps/bridge-wa` — Go + whatsmeow (personal-number QR hosting). HTTP contract in its README.
- **edge** `apps/edge` — tiny redirector for split-link short URLs.
- **embed** — bge-m3 sidecar (1024-dim) for RAG (sub2api has no embeddings endpoint).
- **Shared contracts** `packages/py_contracts` — content blocks, event envelope, LLM client + `[CARD:]`/`[HANDOFF:]`/`[LEAD:]` marker protocol.

## Module map (backend `apps/api/app`)
- `modules/*` — REST routers (auth, inbox, contacts, channels, devices, flows,
  ai, billing, broadcasts, reports, segments, split_links, msg_templates, edm,
  translate, members, workspaces, hooks, widget, openapi_public, settings_mod).
- `channels/` — `ChannelAdapter` interface (`base.py`) + `adapters/*` (17
  channels) + `registry.py` + `ingress_pipeline.py` (inbound: parse → identity →
  conversation → message → outbox → realtime) + `sender.py` (outbound + ingress drain cron).
- `ai/` — `agent_runtime.py` (AI reception, RAG, markers, handoff, points),
  `rag.py`, `consumer.py` (the `ai-agent` service entrypoint), `translation.py`.
- `services/` — `routing.py` (bot→AI→human→unassigned ladder + `route_new_inbound`),
  `messaging.py` (send + snippet + realtime publish), `event_bus.py`, `security.py`
  (JWT + envelope crypto), `redis_client.py`, `bridge_client.py`, `stripe_client.py`.
- `models/` — SQLAlchemy models. `alembic/versions/0001..0005` — migrations (head 0005).
- `main.py` — app factory + widget asset mounts. `seed.py` — plans fixture. `set_plan.py` — self-use plan provisioner.

## Run locally
```bash
# backend deps live in ./.venv (Windows: .venv/Scripts/python)
.venv/Scripts/python -m pytest apps/api -q          # backend tests (~726)
cd apps/web && npm run build                         # tsc + vite (admin SPA)
cd apps/widget && npm run typecheck && npm run test && npm run build
# infra for host-run dev: docker compose -f infra/docker-compose.yml up -d postgres redis minio embed
```

## Deploy / operate production
The full runbook, server access, the switch-from-Chatwoot history, and rollback
are in **[infra/DEPLOY.md](infra/DEPLOY.md)**. Short version (BaoTa web terminal
`https://183.178.215.103:38682/xterm`, project at `/root/smartchat`):
```bash
cd /root/smartchat && git pull origin main
dc build <changed services> && dc up -d            # dc = docker compose -f infra/docker-compose.yml --env-file .env
dc run --rm api alembic -c apps/api/alembic/alembic.ini upgrade head   # if migrations changed
```
Public edge = the BaoTa nginx site for `chat.chilling.com.hk` (config reference:
`infra/nginx/site-chat.conf`) reverse-proxying to the 127.0.0.1-bound containers.

## Where to go next
- Current production status, live accounts, open bugs, roadmap, and the full
  gotcha log → **docs/PROJECT_STATE.md**.
- Per-channel connection requirements → **docs/channel-integration.md**.
- The approved product plan / feature checklist is referenced in PROJECT_STATE.
