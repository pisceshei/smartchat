# SmartChat Visitor Widget (`apps/widget`)

Embeddable chat widget — SmartChat's own implementation of the SaleSmartly-style
聊天外掛. Two build artifacts from one Vite project:

| Artifact | Output | Stack | Budget | Actual |
|---|---|---|---|---|
| **Loader** | `dist/loader.js` | vanilla TS, no framework | < 25 KB raw | ~7.7 KB raw / 3.4 KB gzip |
| **Chat app** | `dist/chat/*` | Preact + TS, runs in iframe | < 120 KB gzip | ~18 KB gzip |

```bash
npm install
npm run build        # loader + chat + size-budget gate
npm run typecheck    # tsc --noEmit
npm test             # vitest (pure logic: store / i18n / protocol)
npm run demo         # zero-dep mock server → http://localhost:8788/host.html
```

`npm run demo` serves the built artifacts plus a fake backend (no WS on
purpose, which exercises the long-poll fallback). Send a message containing
"card" or "button" to get a product-card / quick-buttons reply.

---

## Embed snippet

The web tier serves the loader as `/js/project_{widget_key}.js` — it takes
`dist/loader.js` and replaces the literal string `__WIDGET_KEY__` with the
widget key (see `scripts/dev-server.mjs` for a reference implementation).
The loader also self-resolves the key from its own script URL
(`project_{key}.js` pattern, `?key=` query, or `data-key` attribute), so
serving the file unmodified under the conventional path works too.

```html
<script>
  window.ssq = window.ssq || [];
  // optional overrides (defaults derive from the script's own origin):
  // window.SMARTCHAT_SETTINGS = { apiBase, wsBase, assetBase, lang };
</script>
<script async src="https://chat.example.com/js/project_XXXXXXXX.js"></script>
```

The loader renders a launcher bubble (Shadow DOM, `z-index` 2147483000,
position/color/offsets from the bootstrap config) with an unread badge, plus
a panel hosting the chat iframe at
`{assetBase}/chat/index.html?k={key}&po={parentOrigin}&lang={lang}`.
Desktop: 384×640 rounded panel. Mobile (≤ 640px): full-screen sheet.

## JS SDK surface

Legacy-compatible array-push API (drop-in for SaleSmartly migrations):

```js
ssq.push(['setLoginInfo', { user_id: 'u1', user_name: 'Amy', email: 'a@b.c' }]);
ssq.push(['chatOpen']);
ssq.push(['chatClose']);
ssq.push(['onUnRead', function (count) { /* badge elsewhere */ }]);
ssq.push(['sendTextMessage', 'Hi, I need help with order #123']);
ssq.push(['track', 'add_to_cart', { sku: 'ABC', price: 268 }]);
```

Modern API (same functions, plus helpers):

```js
window.SmartChat.open();
window.SmartChat.close();
window.SmartChat.toggle();
window.SmartChat.setLoginInfo({...});
window.SmartChat.sendTextMessage('...');
window.SmartChat.track('event', {...});
window.SmartChat.onUnread(fn);      // fn(count), called immediately + on change
window.SmartChat.isOpen();
window.SmartChat.getUnread();
```

Commands pushed before the loader/iframe is ready are queued and replayed.
Unknown commands are ignored (forward compatible).

### Auto behaviors

- **page_view tracking**: fired on load and on every `history.pushState` /
  `replaceState` / `popstate` / `hashchange` (deduped by URL), forwarded to
  `POST /api/v1/widget/track` with `{url, title, referrer}`.
- **Unread badge**: chat app counts inbound non-visitor messages while the
  panel is closed, propagates via postMessage; badge + `onUnRead` callbacks.
- **`setLoginInfo`**: forwarded to `POST /api/v1/widget/identify` (or merged
  into session creation), and satisfies the pre-chat requirement.

## Chat app features

- Header: brand name/avatar, online/離線 status dot, connection state.
- Message list renders `MessageContent` blocks (mirror of
  `packages/py_contracts/py_contracts/content.py`): `text` (with URL
  autolink), `media` (image/video/audio/voice/file), `product_card`
  (image/title/subtitle/price + url/postback buttons), `quick_buttons`
  (tappable chips, disabled once answered), `button_reply`, `system_event`.
  Unknown block kinds render as nothing (forward compatible).
- Composer: auto-growing textarea (Enter=send, Shift+Enter=newline,
  IME-composition safe), emoji picker, file upload (20 MB cap), typing
  signal throttled to 1/3s.
- Pre-chat 訪客留資 form: fields from config
  (`text/email/phone/textarea/select`, localized labels, required
  validation); `required_before_chat` blocks the composer until submitted;
  optional forms can be skipped. Submits to `POST /api/v1/widget/lead`.
- Offline mode: banner with configurable notice + email fallback field.
- i18n: zh-Hant + en auto-selected from `navigator.language`
  (any `zh*` → zh-Hant), overridable via config `locale_default`.
- Visitor token persisted in `localStorage` (`sc:{key}:token`) — iframe
  origin storage, invisible to the host page.
- Sends are REST-only with `client_msg_id` idempotency + optimistic echo +
  failed-state retry. Receives via WS with seq/resume; long-poll fallback.
- Theming via CSS custom properties (`--sc-primary` from config); system
  font stack, no external fonts; `show_branding:false` (brand removal,
  ≥ Pro plan) hides the footer.

## Realtime protocol

- WS `GET {wsBase}/ws/widget?token={visitor_token}&resume_from={seq}`
- Server frames: `{"type":"hello","seq":N}` ·
  `{"type":"event","seq":N,"event":{"type":"...","payload":{...}}}` ·
  `{"type":"resync_required"}` · `{"type":"pong"}`
- Client frames (lightweight only — uplink messages never ride the socket):
  `{"type":"ping"}` (25s heartbeat) · `{"type":"typing"}`
- Duplicate suppression: events with `seq <= last seen` are dropped;
  `resync_required` triggers a REST history refetch.
- Fallback: after 3 consecutive WS failures the client long-polls
  `GET /api/v1/widget/events?cursor={seq}&wait=25` (same seq protocol) and
  re-probes WS every 2 minutes.

Events consumed: `message.created`, `message.updated`, `typing`
(`payload.actor != "contact"` shows the agent typing indicator),
`widget.status` (`{is_online}` flips the offline banner live).

## Backend contract (module `apps/api/app/modules/widget` — built by the API agent)

All routes rooted at `/api/v1/widget`; auth = `Authorization: Bearer
{visitor_token}` + `X-Widget-Key` header after session creation.

| Route | Purpose |
|---|---|
| `GET  /bootstrap?key=` | public widget config (shape: `src/shared/config.ts` `WidgetBootstrap`) |
| `POST /session` | `{widget_key, visitor_token?, login_info?, page?, lang?}` → `{visitor_token, contact_id, conversation_id, seq}`; reuses/rotates the anonymous channel identity |
| `POST /identify` | `{login_info}` — ssq `setLoginInfo` → reversible auto-link (plan A.9) |
| `GET  /messages?before=&limit=` | history, ascending `created_at`, each with `seq` |
| `POST /messages` | `{client_msg_id, content: MessageContent}` → `{message, seq}` — idempotent on `client_msg_id` |
| `POST /uploads` | multipart `file` → `{file_id, url, mime, size, name}` (MinIO) |
| `POST /lead` | `{fields, page}` → emits `lead.submitted` |
| `POST /track` | `{event, props, page}` → `visitor.page_view` / custom events |
| `GET  /events?cursor=&wait=` | long-poll fallback → `{events:[{seq,event}], cursor}` |

Media blocks: stored per `py_contracts` (`file_id`), and the API adds a
resolved `url` (plus optional `name`) when serialising for the widget.

## Layout

```
src/loader/index.ts       loader: launcher/badge/panel + bridge + ssq/SmartChat SDK
src/shared/config.ts      WidgetBootstrap types + localized()
src/shared/content.ts     TS mirror of py_contracts MessageContent + WireMessage
src/shared/protocol.ts    postMessage envelope + message unions (both sides)
src/chat/main.tsx         iframe entry (embedded via bridge, standalone via ?k=)
src/chat/controller.ts    session/bridge/realtime orchestration + all actions
src/chat/api.ts           REST client (idempotent send, uploads, long-poll)
src/chat/realtime.ts      WS seq/resume + long-poll fallback state machine
src/chat/store.ts         tiny observable store + hook
src/chat/i18n.ts          zh-Hant/en strings
src/chat/components/      Header / MessageList / Blocks / Composer /
                          PreChatForm / OfflineNotice
scripts/size-check.mjs    build budget gate
scripts/dev-server.mjs    demo host page + mock backend
```
