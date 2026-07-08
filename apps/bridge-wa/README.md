# bridge-wa — whatsmeow WhatsApp-App QR bridge

A single Go process that hosts **many** WhatsApp personal-account sessions
(one whatsmeow client per SmartChat channel account) behind the SmartChat
"device bridge" HTTP contract. It renders QR codes for pairing, relays outbound
sends, and POSTs normalized `InboundEvent`s back to the SmartChat API.

- Listens on `:8100` (compose-internal; published to `127.0.0.1:8100`).
- Management endpoints are gated by `X-Bridge-Auth: <BRIDGE_API_TOKEN>`.
- Sessions persist in a pure-Go SQLite store under `BRIDGE_STORE_DIR`
  (`/data/bridge.db`) — a mounted volume — so pairings survive restarts.
- No CGO: uses `modernc.org/sqlite` registered as the `sqlite` driver, with
  whatsmeow given the `sqlite3` *dialect* via `sqlstore.NewWithDB`.

## HTTP contract

| Method + path                     | Auth              | Body / result |
|-----------------------------------|-------------------|---------------|
| `POST /devices`                   | `X-Bridge-Auth`   | `{device_id, callback_url, callback_secret}` → `201 {device_id, status}` |
| `GET  /devices/{id}/qr`           | `X-Bridge-Auth`   | `200 {qr: string|null, status}` |
| `GET  /devices/{id}/health`       | `X-Bridge-Auth`   | `200 {status, jid?, phone?, pushname?}` |
| `POST /devices/{id}/send`         | `X-Bridge-Auth` **or** `X-Bridge-Signature` | `{to, payload}` → `200 {ok, message_id}` / `4xx-5xx {ok:false, status?}` |
| `POST /devices/{id}/resolve`      | `X-Bridge-Auth`   | `{ids: ["<digits>", …]}` (≤500) → `200 {ok, results: {id: {kind: "lid"\|"pn"\|"unknown", pn?, lid?}}}` — classifies bare digits via the local lid↔phone store (offline-safe, no usync) |
| `POST /devices/{id}/logout`       | `X-Bridge-Auth`   | `200 {ok}` |
| `DELETE /devices/{id}`            | `X-Bridge-Auth`   | `200 {ok}` |
| `GET  /media/{token}`             | token in path     | raw media bytes (inbound-media fetch by the ingress pipeline) |
| `GET  /healthz`                   | none              | `200 {ok:true}` (compose healthcheck) |

`status` ∈ `awaiting_qr | connecting | online | offline | logged_out | banned`.

### `/send`

`payload` is one rendered payload as produced by
`apps/api/app/channels/adapters/bridge.py::BridgeAdapter.render()` →
`{"blocks": [ ... ]}`. Blocks are handled as:

- `text` → a WhatsApp text (conversation) message.
- `media` → resolve bytes (see **Outbound media**), upload to WhatsApp, send
  the matching image/video/audio/document message with the caption.
- Multiple blocks are sent as separate WhatsApp messages; the **last**
  `message_id` is returned.

`/send` auth accepts **either** `X-Bridge-Auth` (the shared token) **or**
`X-Bridge-Signature = hex(hmac_sha256(callback_secret, body))` — the latter is
exactly what the existing `BridgeAdapter.send` signs its request with (using the
account `webhook_secret`, which is the `callback_secret` passed at provisioning
time). Terminal states return `409 {ok:false, status:"logged_out"|"banned"}`
(the Python side maps 409+that status → `AUTH`, pausing the queue); a
not-connected client returns `503 {ok:false, status:"offline"}` (retryable).

## Inbound → SmartChat callback

On every relevant WhatsApp event the bridge POSTs to `callback_url`
(`{SmartChat}/hooks/bridge/{webhook_secret}`) with header
`X-Bridge-Signature = hex(hmac_sha256(callback_secret, body))`, body
`{"events": [ ... ]}`. Shapes match `apps/api/app/channels/base.py`
(`MessageIn`/`DeliveryStatus`/`AccountStatus`) and `py_contracts.content`
(`TextBlock`/`MediaBlock`) exactly, so `parse_normalized_events` consumes them
unchanged.

### `message_in` (text)

```json
{"events":[{
  "kind":"message_in",
  "external_message_id":"3EB0A1B2C3D4",
  "external_user_id":"85291234567",
  "content":{"blocks":[{"kind":"text","text":"hello"}]},
  "external_timestamp":"2026-07-08T12:34:56Z",
  "profile":{"display_name":"Benny","phone":"+85291234567"},
  "media_refs":[]
}]}
```

**LID senders**: when WhatsApp addresses the sender by a `@lid` privacy id, the
bridge resolves the real phone via `SenderAlt` → the local lid↔phone store. If
resolved, `external_user_id`/`profile.phone` are the real number as usual; if
NOT resolvable, `external_user_id` is the lid digits and **`profile.phone` is
omitted** (never a fake `+<lid>`). Either way `meta: {"lid": "<digits>"}` is
attached so the API can key/heal the identity (`ChannelIdentity.meta.wa_lid`).

### `message_in` (media)

`MediaBlock.file_id` is a **required UUID** on the Python side; the bridge emits
a random placeholder that the ingress pipeline rewrites after it downloads the
bytes (via `media_refs`) and stores them in MinIO. The bridge downloads the
encrypted WhatsApp media itself (`client.Download`), caches the plaintext, and
exposes it at `{BRIDGE_PUBLIC_URL}/media/{token}` so `BaseAdapter.fetch_media`
(a header-less `httpx` GET; `ref.kind == "url"`) can pull it. The token is
unguessable and the entry expires after `BRIDGE_MEDIA_TTL` (default 15m).

```json
{"events":[{
  "kind":"message_in",
  "external_message_id":"3EB0...",
  "external_user_id":"85291234567",
  "content":{"blocks":[{
    "kind":"media","media_type":"image","file_id":"7c1e...uuid4...",
    "caption":"look","mime":"image/jpeg","size":48213,"width":800,"height":600
  }]},
  "external_timestamp":"2026-07-08T12:34:56Z",
  "profile":{"display_name":"Benny","phone":"+85291234567"},
  "media_refs":[{
    "block_index":0,
    "ref":{"kind":"url","url":"http://bridge-wa:8100/media/1a2b...","filename":"image.jpg","mime":"image/jpeg"}
  }]
}]}
```

`media_type` maps: image→`image`, video→`video`, audio→`voice` (if PTT) else
`audio`, document→`file`, sticker→`sticker`. Download failure / oversize
(`> BRIDGE_MEDIA_MAX_BYTES`) degrades to a `text` block placeholder so the
message still lands. Group, from-me, status-broadcast and newsletter messages
are skipped.

### `delivery_status`

WhatsApp receipts about our sent messages. One event per message id in the
receipt (unknown ids are parked+expired by the Python side — harmless).

```json
{"events":[{
  "kind":"delivery_status","external_message_id":"3EB0...",
  "status":"delivered","external_user_id":"85291234567",
  "occurred_at":"2026-07-08T12:35:00Z"
}]}
```

`Delivered`→`delivered`, `Read`/`ReadSelf`/`Played`→`read`.

### `account_status`

Emitted on status transitions and on the heartbeat (default every 20s). Ingress
maps `online`→`active`, `offline`→`disconnected`; `logged_out`/`banned` pass
through and pause the send queue.

```json
{"events":[{
  "kind":"account_status","status":"online",
  "detail":{"jid":"85291234567:3@s.whatsapp.net","phone":"+85291234567","pushname":"Benny"}
}]}
```

## Outbound media

An outbound `MediaBlock` reaching the bridge carries `file_id` (a MinIO id) but
no bytes — `BridgeAdapter.render()` does not resolve it to a URL. The bridge
resolves bytes in this order:

1. an explicit `url` on the block (if a future enrich step adds one), else
2. `{SMARTCHAT_FILES_BASE}/api/v1/files/{file_id}` when `SMARTCHAT_FILES_BASE`
   is set (compose defaults it to `ASSETS_BASE_URL`).

If neither yields bytes, the block degrades to its caption text. Inbound media
(customer → us) is the primary QR-driven flow and is fully handled; outbound
media is best-effort and depends on the files endpoint being reachable from the
bridge.

## Config (env)

| Var | Default | Purpose |
|-----|---------|---------|
| `BRIDGE_API_TOKEN` | *(empty=dev, unauthenticated)* | shared `X-Bridge-Auth` token |
| `BRIDGE_LISTEN` | `:8100` | HTTP listen addr |
| `BRIDGE_STORE_DIR` | `/data` | whatsmeow SQLite + registry dir (volume) |
| `BRIDGE_DB_PATH` | `{STORE_DIR}/bridge.db` | SQLite path |
| `BRIDGE_PUBLIC_URL` | `http://bridge-wa:8100` | base for inbound media-fetch URLs |
| `SMARTCHAT_FILES_BASE` | *(empty)* | base for outbound media `file_id` fetch |
| `BRIDGE_MEDIA_TTL` | `15m` | inbound media cache TTL |
| `BRIDGE_MEDIA_MAX_BYTES` | `200MiB` | skip-download threshold |
| `BRIDGE_HEARTBEAT` | `20s` | status reconcile / heartbeat interval |
| `BRIDGE_LOG_LEVEL` | `INFO` | whatsmeow + bridge log level |

## whatsmeow API surface (version-drift notes)

whatsmeow has no stable semver and changes signatures often. The Docker build
runs `go mod tidy` to fetch the **current** whatsmeow + deps and generate
`go.sum`. All whatsmeow calls are confined to `device.go` and `manager.go`; the
version-sensitive call sites are:

- `sqlstore.NewWithDB(db, "sqlite3", log)` + `container.Upgrade(ctx)`
- `container.NewDevice()`, `container.GetDevice(ctx, jid)`
- `whatsmeow.NewClient(store, log)`, `client.GetQRChannel(ctx)`, `client.Connect()`
- `client.SendMessage(ctx, jid, *waE2E.Message)`, `client.Upload(ctx, data, whatsmeow.Media*)`
- `client.Download(ctx, DownloadableMessage)`, `client.Logout(ctx)`, `store.Device.Delete(ctx)`
- `client.Store.LIDs.GetPNForLID(ctx, jid)` / `.GetLIDForPN(ctx, jid)` /
  `.PutLIDMapping(ctx, lid, pn)` — the lid↔phone store used for inbound sender
  resolution and `POST /devices/{id}/resolve`; plus `types.HiddenUserServer`,
  `types.DefaultUserServer`, `types.EmptyJID`, `MessageInfo.SenderAlt`
- proto package `go.mau.fi/whatsmeow/proto/waE2E`; message field names `URL`,
  `Mimetype`, `MediaKey`, `FileEncSHA256`, `FileSHA256`, `FileLength`, `Caption`,
  `FileName`, `PTT`.

If a build surfaces a signature change, the fix is confined to those two files.

## Verified vs. requires-live

- **Compiles** against the resolved whatsmeow API in the Docker build.
- **Requires a real phone scan** post-deploy to verify end-to-end: QR render →
  pair → inbound message/media → `message_in` callback → outbound `/send` →
  `delivery_status`. No emulator can substitute for a WhatsApp account scan.
