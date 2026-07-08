# SmartChat

Omnichannel customer-service & marketing platform — a from-scratch, multi-tenant
SaaS clone of SaleSmartly. Inbox across 17 channels, an embeddable visitor chat
widget, a visual automation flow engine, AI agents with RAG, broadcasts, reports,
and Stripe billing. Live at `https://chat.chilling.com.hk`.

**Agents / new contributors start here:** [CLAUDE.md](CLAUDE.md) (orientation +
golden rules) → [docs/PROJECT_STATE.md](docs/PROJECT_STATE.md) (what's deployed,
open bugs, roadmap, gotchas) → [infra/DEPLOY.md](infra/DEPLOY.md) (deploy runbook).

## Layout
- `apps/api` — FastAPI backend (one image, many entrypoints)
- `apps/web` — React/AntD admin SPA
- `apps/widget` — Preact visitor chat widget (loader + iframe app)
- `apps/bridge-wa` — Go/whatsmeow WhatsApp-App QR bridge
- `apps/edge` — split-link short-URL redirector
- `packages/py_contracts` — shared content/event/LLM contracts
- `infra` — Docker Compose, Dockerfiles, nginx configs, deploy runbook

## Quick start (dev)
```bash
docker compose -f infra/docker-compose.yml up -d postgres redis minio embed
.venv/Scripts/python -m pytest apps/api -q         # backend tests
cd apps/web && npm run build                        # admin SPA
cd apps/widget && npm run build                     # widget
```
