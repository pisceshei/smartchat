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
- **Stack**: Docker Compose (`infra/docker-compose.yml`), **14 services**:
  postgres redis minio · api ws-gateway worker beat flow-engine **channel-ingress**
  **ai-agent** edge web · embed bridge-wa. channel-ingress = the blocking
  ingress:* stream consumer (webhook inbound lands in ms; without it inbound
  waits for the worker's 15s drain cron — the "inbox is 10-15s late" symptom).
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
- **Inbound latency**: webhook → inbox in milliseconds via the `channel-ingress`
  service (verified with a simulated Telegram update: message + AI reply landed
  within seconds; before, everything waited on the 15s drain cron).
- **Unread badge**: shows a stable count, zeroes when any member opens the
  conversation, recounts from later messages (verified live, no flicker).
- **客戶 page** (round 8, commit `bff29c7`): loads without crashing; list rows show
  real 社媒渠道 icons / 接待成員 / 郵箱 / 最後活躍; 添加客戶 modal → POST /contacts
  201 → row appears (verified UI + DB + api log). Filters/export contract mapped
  (`empty→not_exists`, `tag→tag_id`, …; export downloads a CSV blob).
- **Inbox default view**: sidebar lists **AI 成員 first** and it is the landing
  tab (`InboxPage` defaults to `"ai"`).

## 5. Open items / known bugs (pick these up)
- **Round 11 (2026-07-09): widget auto social-entry + official-API channels to production grade**
  - *widget 自動社媒入口*: connect a channel → the visitor widget's Home tab
    automatically gains a "透過以下方式聯絡我們" entry that deep-links to it.
    Backend `widget/service.py::channel_contact_entry` derives a SAFE public link
    per channel (email→mailto, whatsapp_bsp→wa.me/`external_id`, whatsapp_cloud→
    wa.me/`health.display_phone_number`, telegram_bot→t.me/`health.username`,
    line_oa→line.me/R/ti/p/`health.basic_id`, messenger→m.me/`external_id`;
    wechat_kf→copy handle); `_assemble_social` filters by the per-widget
    `config.social` allow-list and injects a `social` block into the public
    bootstrap. **Privacy contract**: the bootstrap endpoint is unauthenticated,
    so ONLY the derived link/handle is exposed — never external_id/health/creds.
    Personal-number/internal channels (`whatsapp_app`) are default-hidden (opt-in
    via `social.shown`). Widget renders `.sc-social` in `HomeScreen.tsx` with
    inline-SVG brand glyphs (`SocialIcons.tsx`, no icon lib — chat bundle still
    24.7KB gzip); editor gains a master `social_enabled` switch (home tab), with
    hidden/shown/order/labels round-tripped through `...cfg.social`.
  - *official-API channels → production grade* (all fixes tested):
    - **Zalo credential-key bug (🔴 was silently killing prod Zalo)**: modal
      posted `app_secret` but the backend reads `oa_secret` everywhere → webhook
      signature verification skipped + ~1h token never refreshed. Fixed the modal
      field name + backend `app_secret` fallback (adapter + hook).
    - **email connect was broken end-to-end**: modal `host/port/user/auth_type`
      landed in `config` but the adapter reads them from ENCRYPTED credentials.
      Rewrote the email connect branch to remap modal fields → adapter keys
      (`username`→imap_user/smtp_user, `password`→imap_password/smtp_password,
      `auth_type`+`oauth_*`→credentials); OAuth modal gained client_id/secret/
      token_endpoint (provider→endpoint derived server-side).
    - **line_oa inbound was dead**: connect now calls `adapter.set_webhook` and
      surfaces `/hooks/line/{secret}` (added to `_HOOK_PATH`/needs_hook_secret);
      also mirrors `channel_access_token`→`access_token` (adapters read the
      latter — a second latent break).
    - **messenger/instagram**: connect auto-calls `POST /{page}/subscribed_apps`;
      instagram via-Page now resolves + stores the linked IG account id as
      `external_id` (IG webhooks route by `entry.id` = IG id, not page id → was
      dropping all via-Page inbound).
    - **cross-cutting token rot**: `refresh_credentials` was implemented but never
      called in prod. Added `sender.py` crons: `youtube_poll_task` (YouTube has no
      webhook — polls comments every 2m + persists cursor), `refresh_tokens_task`
      (proactive OAuth refresh ~5m before expiry, email/youtube/zalo),
      `health_probe_task` (periodic re-probe → status/health for the UI). Removed
      dead `_CRED_FIELDS`.
  - *left for a future round (documented in the gallery cards)*: `line_app`
    (backend scaffolded but needs a real LINE personal bridge + `bridge_line_url`
    — LINE has no official personal API, ToS-risky), `telegram_app` (needs a
    Telegram MTProto/TDLib user-account bridge — technically legit but new infra),
    `tiktok_app` / personal `wechat` (no sanctioned personal messaging API — keep
    `connectable:false`). `tiktok_business` is production-grade for COMMENTS; DM is
    an external Meta/TikTok allow-list gate, not our code.
- **Round 10 (2026-07-09): widget embed/save fix bundle + full YCloud BSP integration**
  - *widget embed*: the loader's chat iframe pointed at `/chat/index.html` but the
    api mounts `/widget-app` and nginx had no `/chat` route → the SPA catch-all
    served the ADMIN LOGIN page inside the widget panel (matched the user's shop
    screenshot). Fixed: `loader/chatUrl.ts` emits `/widget-app/index.html`
    (contract-tested); the api double-mounts `/chat` as a legacy alias for
    loaders cached up to a day; nginx gained `location ^~ /chat/` plus THREE
    Chatwoot-leftover neutralizers (`= /widget` blank page, `^~ /packs/` 204,
    `= /cable` 204) so un-removed old embeds can never render the login page
    again. The shop's old Chatwoot snippet still should be deleted (DEPLOY.md §7).
  - *widget save*: 儲存 silently no-oped when a hidden-tab field failed
    validation. Now: fully-empty banner/prechat rows are auto-pruned before
    validation, errors name the tab/row/field and jump there, the live preview
    seeds from saved values (no more "SmartChat" default flash), and the form
    re-hydrates after save. Extracted to `widgetConfigForm.ts` + vitest. The
    loader route now 404s (no-store) unknown/disabled widget keys, fail-open on
    DB errors; `backend.Dockerfile` runs the size-budget gate.
    NOTE: `allowed_domains` remains record-only (enforced nowhere) — hint text
    says so; enforcement is future work.
  - *YCloud BSP (whatsapp_bsp) now end-to-end*: NEW `POST /hooks/ycloud`
    (app-level; routes by business number with `_plus` normalization; verifies
    `YCloud-Signature` t/s HMAC fail-closed when a secret is stored, warn-accept
    otherwise; template.reviewed applied directly to MsgTemplate). Connect
    auto-registers the webhook endpoint (GET-dedup → POST; secret →
    encrypted credentials via the new `ConnectResult.credentials_patch`;
    sibling-account secret copy for second numbers; manual-console fallback
    surfaced in the modal). Two-step connect UX: api_key → 載入號碼 → pick
    (fixes the phone_number_id/phone_number mismatch; waba_id persisted).
    Media fetch sends X-API-Key (refs are kind=ycloud_media). Template
    lifecycle: sync widened to BSP (`import_missing` pulls console-built
    templates in), NEW `POST /msg-templates/whatsapp/{id}/submit` creates the
    template on YCloud (409 → adopts the existing remote), review webhook +
    6h cron reconcile statuses. Broadcasts: TEMPLATE_CHANNEL_MAP +
    WINDOW/TEMPLATE_CHANNELS now include whatsapp_bsp; ratelimit 20/s.
    Frontend: `galleryType()` maps whatsapp_cloud/whatsapp_bsp → the
    WhatsApp API card (un-bricks the broadcast tile + WABA selectors);
    `_serialize_account` now returns `display_name`.
    DEPLOY: server .env must have `PUBLIC_BASE_URL=https://chat.chilling.com.hk`
    (drives webhook auto-registration).
- **Round 9 (2026-07-09): WhatsApp lid-as-phone + outbound/AI realtime rendering**
  (deployed; production E2E per the checklist below). Two user-reported bugs:
  a WhatsApp contact showed phone `+56985642876983` — a **lid**, not a phone
  (real number +85266577437); and AI replies rendered as **empty bubbles** in
  the admin inbox (and were dropped on the widget) until a manual refresh,
  with delivery ticks frozen at the spinner.
  - *lid fix (bridge)*: `handleMessage` resolves the sender phone via
    `SenderAlt` → `Store.LIDs.GetPNForLID` (local store, no usync); an
    unresolved sender gets **no phone at all** (never `"+<lid>"`) and the lid
    always travels as `meta.lid`. New `POST /devices/{id}/resolve` classifies
    digits (lid/pn/unknown) from the store for the backfill. The 9da5793
    send-retry now picks its target from the store (`lidRetryTarget`) so a
    transient usync 429 on a REAL phone can no longer poison `lidRecipients`.
  - *lid fix (API)*: `_upsert_identity` reconciles — fresh unresolved lid
    (phone stays empty, UI shows "-"), **heal** (later phone-keyed event
    re-keys the lid identity in place, fixes the `"+<lid>"` placeholder,
    emits contact.updated), **duplicate** (lid- and phone-keyed identities →
    `merge_contacts`, placeholder cleared first). `sender._bridge_to`
    addresses unresolved lid identities as `<lid>@lid` explicitly. Backfill:
    `dc run --rm api python -m apps.api.app.backfill_wa_lid_phones`
    (dry-run default; `--apply --map <lid>=<+phone> --clear-phone <lid>`).
  - *realtime fix (backend)*: outbound `message.created` now nests the full
    row under `payload.message` via the shared builder
    `messaging.message_row_payload()` (also used by ingress — one shape,
    contract-locked by `tests/realtime/test_message_event_contract.py`), and
    adds flat `id`/`conversation_id`/`created_at` so the visitor whitelist
    passes a complete row to the widget. Delivery-status transitions
    (`_finalize_sent`/`_finalize_failed`/`apply_delivery_status`/read
    watermark) now `publish_realtime` after commit → ticks advance live.
    Root cause of the empty bubble: the gateway slims flat `content` off
    message frames for non-open conversations, the SPA never sets
    `open_conversation_id`, and only the nested row survives the slim —
    outbound had no nested row (inbound did, which is why inbound worked).
  - *realtime fix (frontend)*: `applyEvent.messageFromEvent` never fabricates
    an empty-blocks row again (synthesizes a text block from `text_plain` +
    refetches, or falls to invalidate); `message.updated` is **patch-only**
    (`patchMessage`) — also fixes the latent translate-wipe. Widget
    `messageFromPayload` accepts the whitelisted flat `id`, synthesizes from
    `text_plain`, and patches `delivery_status`. Round-7 badge semantics and
    the visitor-echo-undeliverable invariant are preserved (locked by
    `apps/web/src/realtime/applyEvent.test.ts` — first web vitest — and
    `apps/widget/tests/messageFromPayload.test.ts`).
  - *deploy-order note*: bridge-wa must go out together with (or before) the
    api — an OLD bridge sends the fake `"+<lid>"` phone with no `meta.lid`,
    which the new API cannot recognize for brand-new senders (it CAN protect
    identities already annotated with `meta.wa_lid`). Run the backfill after
    the bridge is up. Adversarial review also hardened: duplicate-contact
    merges run in their own transaction AFTER the message commit (canonical
    ordered row locks — a merge deadlock can never drop the customer's
    message), and the backfill planner refuses two migrates onto one phone
    (identity unique key).
- **Round 5 (2026-07-08, commit `82e074f`, E2E-verified in prod): four fixes** —
  (1) *Inbox realtime*: ingress now publish_realtime()s inbound messages (side
  effects unified via `messaging.register_inbound_message`), `client_frame`
  emits canonical compat keys (id/payload/conversation_id), ws.ts normalizes
  frames + lazy token + tryRefresh on 4401, applyEvent rebuilds from flat
  fields and always degrades to invalidateQueries; widget accepts the canonical
  envelope. Verified: visitor message → list bump/snippet/unread + thread all
  update live, AI reply streams in live, zero refreshes. (2) *Notifications*:
  one-time permission prompt (user-gesture button), desktop default ON once
  granted (persist v1 migration), popup whenever not viewing that conversation.
  User still needs to click 開啟通知 once to grant browser permission.
  (3) *AI 託管 semantics*: bot_managed is the single switch — human interjection
  no longer pauses the AI (verified live), kb_miss/[HANDOFF:no_context]/
  llm_error/external_error fail soft and stay managed, keyword + explicit model
  handoffs still hard-off (verified: model emitted HANDOFF:discount_confirmation
  on a discount question → toggle visibly OFF + note), toggling 託管 back ON
  re-attaches an AI member (verified: AI resumed replying). (4) *Telegram*: dup
  check now precedes setWebhook (secret-rotation black hole), unmatched-secret
  drops log at error level. ~~The production bot account is still soft-deleted~~
  → **resolved in round 6**: user re-added @chilllove_bot, webhook/secret verified.
- Minor UI nit (new): after a conversation.updated realtime patch the header
  can show 未命名訪客 while the list keeps the contact name — display-name field
  probably clobbered by a partial patch; cosmetic, refresh restores it.
- **Rounds 6-8 (2026-07-08, all deployed + E2E-verified in prod)**:
  (6) `c219816` — inbound was 10-15s late because the only ingress consumer was
  the 15s drain cron; added the dedicated **`channel-ingress`** compose service
  (blocking `run_ingress_loop`, entrypoint
  `apps/api/app/channels/ingress_consumer.py`). Also fixed the Telegram connect
  black hole (dup-409 check now precedes setWebhook so a rotated secret can't be
  half-persisted) — user re-added @chilllove_bot, webhook/secret verified, full
  chain proven with a simulated update. Remaining: a real-phone message to
  @chilllove_bot as final acceptance.
  (7) `9154ed2` — unread badge flickered and could never clear: three writers
  fought over the count and only the assignee's read zeroed it. Now
  `conversation.updated` is the single authority and `advance_read_cursor`
  zeroes `agent_unread_count` for ANY member.
  (8) `bff29c7` — 客戶 page crash + enrichment + AI-default view (see §4).
  Contract regression test: `apps/api/tests/contacts/test_query_contract.py`.
- `fix/outbound-dispatch` is **merged into `main`**; the server is back on the
  standard main-pull deploy flow.
- One stale test message remains `delivery_status=failed` (exhausted retries
  before the bridge fix); requeue with
  `UPDATE messages SET delivery_status='pending', delivery_error=NULL WHERE delivery_status='failed' AND delivery_error='RETRYABLE';`
  if you want it delivered — the drain cron picks it up.
- **WhatsApp LID outbound** (fixed 2026-07-08, commit `9da5793`): identities
  created from LID-addressed inbound with empty `SenderAlt` store the lid digits;
  phone addressing (`<lid>@s.whatsapp.net`) fails "no LID found". The bridge now
  retries such sends via `@lid` (Signal session already exists from inbound) and
  caches the discovery per device. Verified: `[Bridge INFO] recipient … is a lid,
  not a phone — delivered via @lid`, messages went sent→delivered→read.
  **Extended in round 9**: the stored identity/phone itself is now healed (the
  9da5793 fix only made delivery work — the contact kept displaying `+<lid>`).
- **Outbound replies were never dispatched to the channel** (fixed 2026-07-08 on
  branch `fix/outbound-dispatch`, E2E-verified): `messaging.send_message`
  writes the message `delivery_status='pending'` and emits `message.created` with
  `requires_channel_send=True`, but **nothing consumed that flag** to call
  `enqueue_send`. Only `marketing/fanout` enqueued; the three interactive paths —
  inbox agent reply (`modules/inbox/router.py`), AI reply
  (`ai/agent_runtime.process_event`), and flow send actions (`flow_engine`) —
  committed + published realtime but never enqueued. **Widget worked** (its
  delivery is WS fan-out, adapter `send` is a no-op); every real channel left
  replies at `pending` forever, so AI answers never reached the customer. Fix =
  the two-layer design the `messaging.py` docstring describes: (a) hot path
  `messaging.dispatch_channel_sends(events)` enqueues after commit+publish in all
  three callers; (b) safety net `sender.drain_pending_sends_task` cron drains
  unclaimed `pending` outbound rows every 15s (`run_at_startup` flushes any
  backlog). Idempotent via the per-message Redis claim in `send_outbound_message`.
  Regression test: `apps/api/tests/channels/test_outbound_dispatch.py`.
- **WhatsApp App inbound** (fixed 2026-07-08, verify with real phones): two bugs —
  (a) first message after pairing arrived on a cold Signal session as
  "Unavailable"/undecryptable → no event → no callback; fixed by
  `AutomaticMessageRerequestFromPhone=true` + handling `events.UndecryptableMessage`
  + LID `SenderAlt` (`apps/bridge-wa/device.go`). (b) text inbound was dropped by
  `media_refs: null` failing the `list[MediaRef]` validator; fixed on both sides
  (Go `omitempty`+non-nil slice, Python `field_validator` null→default in
  `apps/api/app/channels/base.py`). ~~Final acceptance pending~~ → **done**: real-phone
  WhatsApp inbound + AI reply + ✓✓ read receipts verified in prod (round 4/§4).
- Visitor optimistic echo occasionally duplicates (pending bubble not merged with
  the server message via client_msg_id).
- RAG **query** embedding calls sub2api `/v1/embeddings` (404) and falls back to
  lexical search — repoint query embeddings at `EMBED_BASE_URL` (ingest already uses it).
- Widget home banners use picsum placeholders — user to set real images (admin →
  聊天外掛 → edit → 首頁模式). Sample KB products to be replaced with real ones
  (future: a Fecify→KB product sync).
- Telegram/WhatsApp inbound → inbox with the right channel icon: WhatsApp
  verified with real phones; Telegram verified via simulated update — final
  validation needs the user's phone (message @chilllove_bot after START).
- A test contact 測試新客戶王小明 (`wang@test.com`) was created during the round-8
  add-customer verification and left in place (like the earlier `tester` row) —
  delete from the UI if unwanted.

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
- **Worker never ran the channel I/O crons**: `apps/api/app/jobs/worker.py` only
  imported `channels/sender.py` lazily (inside `marketing.fanout`), so the worker
  process never registered `ingress_drain` / `send_outbound_message` / email poll
  / requeue → EVERY real channel silently broke (widget was fine; it's synchronous
  WS). Fixed by importing `..channels.sender` at the top of `worker.py` (commit
  `694b8f8`). Lesson: a `@task`/`register_cron` only takes effect if the worker
  imports the module.
- **Outbound never dispatched to the channel**: a `pending` outbound message only
  reaches its channel if someone calls `sender.enqueue_send(message_id)` after
  commit — and that call lived ONLY in `marketing/fanout`. Agent/AI/flow replies
  emitted the `requires_channel_send=True` event but never enqueued (no consumer
  of that flag existed). Symptom: real-channel replies stuck
  `delivery_status=pending`, bridge outbound log empty, widget fine. Fixed with
  `messaging.dispatch_channel_sends` (hot path, all three callers) +
  `sender.drain_pending_sends_task` (15s pending-drain backstop). Lesson: every
  `send_message` to a real channel needs a post-commit dispatch; the drain cron
  is the safety net if a future caller forgets.
- **Widget boot skeleton stuck**: `useAppState` missed store updates that landed
  between first render and effect-subscribe (same-origin bootstrap resolves in
  ~100ms) — re-read `store.get()` after subscribing.
- BaoTa web xterm chokes on long streaming output — redirect builds to a log file
  and background them, then poll (`nohup … > _build.log 2>&1 &`).

## 7. Commit history anchors (recent)
`bff29c7` customers crash+enrichment + AI-default inbox view · `9154ed2` unread
badge single-authority · `c219816` channel-ingress service + telegram connect
order · `ed250d9` outbound-dispatch fix (merged to main) · `694b8f8`
worker channel-I/O crons import · `9d18109` prod frontend builds · `956647e` drop
dead COPY fixtures · `f6d80b5` embed torch 2.6 · `44b070c` deploy runbook ·
`6306b75` channel+widget+AI+home-mode fixes · `116e0ca` AI consumer actor
fallback · `c2015c8` bridge rerequest/LID · `ba2da56` media_refs:null fix. Always
`git log --oneline` for the latest.
