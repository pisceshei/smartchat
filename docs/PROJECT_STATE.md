# SmartChat — project state & handoff (living document)

Keep this current. It is the single source of truth for a fresh agent taking
over. Last updated: 2026-07-08.

---

## 1. What this is / decisions already made
100% functional clone of SaleSmartly (omnichannel CS + marketing SaaS), built
from scratch (NOT Chatwoot), multi-tenant (Free/Pro/Max/Custom + AI-points
metering). Deployed to the user's BaoTa production server, replacing an old
Chatwoot deployment, on the SAME domain `chat.chilling.com.hk`.

- **AI** goes through **sub2api** (`https://sub2api.chilling.com.hk`, Anthropic
  protocol relay). Tiers: fast=`claude-haiku-4-5-20251001`, smart=`claude-sonnet-4-6`.
  sub2api has **no embeddings endpoint** → the `embed` sidecar (bge-m3, 1024-dim)
  serves RAG embeddings.
- **Payments** = Stripe (hosted Checkout so the frontend needs no publishable
  key); key entered by the user in the admin backend after deploy.
- Approved product plan / full feature checklist:
  `C:\Users\pisce\.claude\plans\https-app-salesmartly-com-next-automatio-foamy-cake.md`
  (on the original dev machine). Appendix A = data model + channel gateway,
  Appendix B = flow engine / AI / broadcast / reports. If unavailable, this repo
  + README + docs are self-sufficient.

## 2. Production deployment (live)
- **Server**: BaoTa panel, `183.178.215.103`, web terminal
  `https://183.178.215.103:38682/xterm` (user-authorized). Also runs Odoo 19,
  the `collect` tool, Typesense (separate box), etc. — don't disturb those.
- **Project dir**: `/root/smartchat` (git clone of `github.com/pisceshei/smartchat`, branch `main`).
- **Stack**: Docker Compose (`infra/docker-compose.yml`), 15 services:
  postgres redis minio · api ws-gateway worker beat flow-engine **ai-agent** edge web · embed bridge-wa.
  All app ports bound to `127.0.0.1`; the BaoTa nginx site is the public edge.
- **Secrets**: `/root/smartchat/.env` (hand-written; NOT in git). Contains DB/MINIO
  passwords, `SECRET_KEY`, `CREDENTIALS_MASTER_KEY` (**never change**), sub2api
  LLM config, `BRIDGE_API_TOKEN`. Stripe/Meta/Slack keys are blank there and set
  in the admin UI. `infra/.env.prod.example` documents every var (with a real LLM
  key, hence gitignored).
- **DB**: alembic head **0005**; seeded 4 plans. `pgvector` for `kb_chunks vector(1024)`.
- **Old Chatwoot**: stopped (`/root/chilling-chat`, containers down, volumes kept
  ~30 days for rollback). Backup at `/root/_backup_chatwoot_2026-07-08/`
  (`pg.sql` 439K, `chilling-chat.tgz`, `chat.nginx.conf.bak`). Rollback = restore
  that nginx conf + `docker compose up` the old dirs.
- **Repo visibility**: was made public temporarily for the server `git clone`.
  Recommend making it **private** again (secrets are not committed, but ops
  details + server IP are).

## 3. Self-use account
- Login `cs@chilling.com.hk` (the user first registered `cs@chilling.comm.hk`
  by typo — corrected in the DB). Workspace **CHILL LOVE**, plan **Max / 720d**
  (expires 2028-06-27), applied via `set_plan.py`.
- The first registered user of a workspace becomes its **super_admin** (`"*"`
  permission). To grant Max to any account without charge:
  `dc run --rm -v /root/smartchat/apps/api/app/set_plan.py:/srv/smartchat/apps/api/app/set_plan.py api python -m apps.api.app.set_plan <email> max 720`.

## 4. Verified working in production (E2E)
- Admin: login, inbox (3-pane, channel icons + snippet preview + unread), channel
  connect (real errors surfaced), widget CRUD, billing shows Max.
- **Channels connected**: widget (`6d0c44de280b1fc3`), Telegram bot
  (`@chilllove_bot`, 8585479604), WhatsApp App (QR-paired `85266577437`).
- **Visitor widget (SaleSmartly "home mode")**: home screen (brand + banner ad +
  New Conversation + Home/Chat tabs) → lead form (name/email, posted as a visitor
  message) → **AI agent "Angel" auto-receives via sub2api** (Traditional Chinese,
  RAG over the KB) → **product cards** (image/name/price, click → product page) →
  human-handoff keywords (真人/人工/human) configured. zh-Hant/zh-CN/en auto-detected.
- KB collection "商品目錄" seeded with 3 sample products (handles
  `lavender-candle`/`rose-diffuser`/`sleep-spray`).

## 5. Open items / known bugs (pick these up)
- **WhatsApp App inbound** (fixed 2026-07-08, verify with real phones): two bugs —
  (a) first message after pairing arrived on a cold Signal session as
  "Unavailable"/undecryptable → no event → no callback; fixed by
  `AutomaticMessageRerequestFromPhone=true` + handling `events.UndecryptableMessage`
  + LID `SenderAlt` (`apps/bridge-wa/device.go`). (b) text inbound was dropped by
  `media_refs: null` failing the `list[MediaRef]` validator; fixed on both sides
  (Go `omitempty`+non-nil slice, Python `field_validator` null→default in
  `apps/api/app/channels/base.py`). **Final acceptance = send a real WhatsApp
  message to the paired number and confirm it lands in the inbox with AI reply.**
- Visitor optimistic echo occasionally duplicates (pending bubble not merged with
  the server message via client_msg_id).
- RAG **query** embedding calls sub2api `/v1/embeddings` (404) and falls back to
  lexical search — repoint query embeddings at `EMBED_BASE_URL` (ingest already uses it).
- Widget home banners use picsum placeholders — user to set real images (admin →
  聊天外掛 → edit → 首頁模式). Sample KB products to be replaced with real ones
  (future: a Fecify→KB product sync).
- Telegram/WhatsApp inbound → inbox with the right channel icon is code-complete;
  final validation needs the user's phone.

## 6. Gotcha log (incidents → fixes; prevents repeating them)
- **compose interpolation**: `-f infra/…` without `--env-file .env` → default
  `POSTGRES_PASSWORD`/`MINIO_ROOT_PASSWORD` → `password authentication failed`.
  Always pass `--env-file .env`. (Recreated the pg/minio volumes to fix.)
- **`COPY fixtures`** in backend.Dockerfile referenced an empty untracked dir →
  fresh clone build failed; removed (real fixtures live in `apps/api/app/fixtures`).
- **embed torch**: needs `torch>=2.6` (transformers blocks `torch.load` on <2.6
  for CVE-2025-32434). Pinned 2.6.0 in `infra/embed.Dockerfile`.
- **`.env.prod.example` gitignored** by the `.env.*` rule → not in the clone;
  write `/root/smartchat/.env` by hand (heredoc). Don't try to force-add it (real key).
- **Chrome autofill** poisoned channel-connect password fields with the saved
  login password → "all channels fail". Fixed with autocomplete guards + decoy
  inputs in `ConnectModals.tsx`; also surface the real backend error, not a generic toast.
- **Widget config editor** read a nested shape the backend didn't return → crashed
  the whole SPA (no ErrorBoundary). Fixed: editor reads/writes nested `config`;
  added a global ErrorBoundary in `App.tsx`.
- **Soft-delete + unfiltered lists**: widget/channel delete only set `enabled=false`
  but lists didn't filter → "can't delete"; quota counted disabled rows → "can't
  add a 2nd". Fixed to filter `enabled=true`.
- **30-min logout**: access token TTL 30 min and the SPA never used the refresh
  token — added single-flight `/auth/refresh` retry on 401 in `api/client.ts`.
- **AI never replied**: (1) `route_new_inbound` had no caller — wired into
  `ingress_pipeline`; (2) no process ran the AI consumer group — added the
  `ai-agent` compose service (`apps/api/app/ai/consumer.py`); (3) `process_event`
  only read `payload.sender_type` but ingress events carry the sender in
  `event.actor.type` — fixed to fall back to actor.
- **Widget boot skeleton stuck**: `useAppState` missed store updates that landed
  between first render and effect-subscribe (same-origin bootstrap resolves in
  ~100ms) — re-read `store.get()` after subscribing.
- BaoTa web xterm chokes on long streaming output — redirect builds to a log file
  and background them, then poll (`nohup … > _build.log 2>&1 &`).

## 7. Commit history anchors (recent)
`9d18109` prod frontend builds · `956647e` drop dead COPY fixtures · `f6d80b5`
embed torch 2.6 · `44b070c` deploy runbook · `6306b75` channel+widget+AI+home-mode
fixes · `116e0ca` AI consumer actor fallback · `c2015c8` bridge rerequest/LID ·
`ba2da56` media_refs:null fix. Always `git log --oneline` for the latest.
