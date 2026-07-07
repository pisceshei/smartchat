/**
 * Zero-dependency demo/mock server for the widget. Serves the BUILT artifacts
 * (run `npm run build` first) plus a fake widget backend, so the full embed
 * flow can be exercised without the real API:
 *
 *   node scripts/dev-server.mjs   →  http://localhost:8788/host.html
 *
 * - /host.html                       demo merchant page with the embed snippet
 * - /js/project_demo123.js           loader with __WIDGET_KEY__ injected
 * - /chat/*                          built chat app
 * - /api/v1/widget/*                 mock REST backend (session/messages/
 *                                    events long-poll/uploads/lead/track)
 * No WS endpoint on purpose — exercises the long-poll fallback path.
 */
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PORT = Number(process.env.PORT || 8788);
const DIST = fileURLToPath(new URL("../dist", import.meta.url));
const KEY = "demo123";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
};

let seq = 0;
let msgId = 0;
const eventQueue = []; // {seq, event}
const messages = [];

function pushEvent(event) {
  seq += 1;
  eventQueue.push({ seq, event });
}

function agentReply(text, extraBlocks = []) {
  msgId += 1;
  const m = {
    id: "srv-a" + msgId,
    sender_type: "member",
    sender_name: "Demo Agent",
    content: { blocks: [{ kind: "text", text }, ...extraBlocks] },
    created_at: new Date().toISOString(),
  };
  messages.push(m);
  pushEvent({ type: "message.created", payload: { message: m } });
}

const BOOTSTRAP = {
  widget_key: KEY,
  brand: {
    name: "Demo Shop",
    avatar_url: null,
    welcome_text: { en: "Welcome! Ask us anything.", "zh-Hant": "歡迎！有任何問題請隨時提問。" },
  },
  appearance: { position: "right", primary_color: "#4F46E5", show_branding: true },
  locale_default: "en",
  pre_chat: {
    enabled: true,
    required_before_chat: false,
    fields: [
      { key: "name", type: "text", label: { en: "Name", "zh-Hant": "姓名" }, required: true },
      { key: "email", type: "email", label: { en: "Email", "zh-Hant": "電子郵件" }, required: false },
    ],
  },
  offline: { is_online: true, email_fallback: true },
  features: { file_upload: true, emoji: true },
};

const HOST_HTML = `<!doctype html><html><head><meta charset="utf-8"><title>Demo host page</title></head>
<body style="font-family:sans-serif;padding:40px">
<h1>Merchant demo page</h1>
<p>The SmartChat widget should appear bottom-right.</p>
<button onclick="ssq.push(['chatOpen'])">ssq chatOpen</button>
<button onclick="ssq.push(['sendTextMessage','hello from SDK'])">ssq sendTextMessage</button>
<button onclick="ssq.push(['setLoginInfo',{user_id:'u1',user_name:'Benny',email:'benny@example.com'}])">ssq setLoginInfo</button>
<script>
window.ssq = window.ssq || [];
ssq.push(['onUnRead', function(n){ document.title = (n>0 ? '('+n+') ' : '') + 'Demo host page'; }]);
window.SMARTCHAT_SETTINGS = { apiBase: location.origin, assetBase: location.origin };
</script>
<script async src="/js/project_${KEY}.js"></script>
</body></html>`;

function json(res, body, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" });
  res.end(JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        resolve({});
      }
    });
  });
}

createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;

  if (p === "/" || p === "/host.html") {
    res.writeHead(200, { "Content-Type": MIME[".html"] });
    return res.end(HOST_HTML);
  }

  if (/^\/js\/project_[A-Za-z0-9_-]+\.js$/.test(p)) {
    const loader = readFileSync(join(DIST, "loader.js"), "utf8");
    res.writeHead(200, { "Content-Type": MIME[".js"] });
    return res.end(loader.replace("__WIDGET_KEY__", KEY));
  }

  if (p.startsWith("/chat/")) {
    try {
      const file = p === "/chat/" ? "/chat/index.html" : p;
      const buf = readFileSync(join(DIST, file));
      res.writeHead(200, { "Content-Type": MIME[extname(file)] || "application/octet-stream" });
      return res.end(buf);
    } catch {
      res.writeHead(404);
      return res.end("not found");
    }
  }

  // ---- mock widget API ----
  if (p === "/api/v1/widget/bootstrap") return json(res, BOOTSTRAP);

  if (p === "/api/v1/widget/session" && req.method === "POST") {
    const body = await readBody(req);
    return json(res, {
      visitor_token: body.visitor_token || "demo-token-" + Math.random().toString(36).slice(2),
      contact_id: "c-demo",
      conversation_id: "conv-demo",
      seq,
    });
  }

  if (p === "/api/v1/widget/messages" && req.method === "GET") {
    return json(res, { messages });
  }

  if (p === "/api/v1/widget/messages" && req.method === "POST") {
    const body = await readBody(req);
    msgId += 1;
    const m = {
      id: "srv-" + msgId,
      sender_type: "contact",
      content: body.content,
      client_msg_id: body.client_msg_id,
      created_at: new Date().toISOString(),
    };
    messages.push(m);
    pushEvent({ type: "message.created", payload: { message: m } });
    const text = (body.content?.blocks || []).map((b) => b.text || "").join(" ");
    setTimeout(() => pushEvent({ type: "typing", payload: { actor: "member" } }), 400);
    setTimeout(() => {
      if (/card/i.test(text)) {
        agentReply("Here is our bestseller:", [
          {
            kind: "product_card",
            title: "Hydra Boost Serum 50ml",
            subtitle: "Deep hydration for all skin types",
            image_url: "https://placehold.co/400x250",
            price: "268.00",
            currency: "HK$",
            url: "https://example.com/p/serum",
            buttons: [
              { text: "Buy Now", action: "url", value: "https://example.com/p/serum" },
              { text: "More info", action: "postback", value: "info:serum" },
            ],
          },
        ]);
      } else if (/button/i.test(text)) {
        pushEvent({ type: "message.created", payload: { message: (msgId += 1, {
          id: "srv-a" + msgId,
          sender_type: "member",
          sender_name: "Demo Agent",
          content: { blocks: [{ kind: "quick_buttons", text: "What do you need help with?", buttons: [
            { id: "orders", text: "My orders" },
            { id: "shipping", text: "Shipping" },
            { id: "human", text: "Talk to human" },
          ] }] },
          created_at: new Date().toISOString(),
        }) } });
      } else {
        agentReply("Echo: " + text);
      }
    }, 1500);
    return json(res, { message: m, seq });
  }

  if (p === "/api/v1/widget/events") {
    const cursor = Number(url.searchParams.get("cursor") || 0);
    const waitMs = Math.min(Number(url.searchParams.get("wait") || 25), 25) * 1000;
    const started = Date.now();
    const poll = () => {
      const fresh = eventQueue.filter((e) => e.seq > cursor);
      if (fresh.length > 0 || Date.now() - started > waitMs) {
        return json(res, { events: fresh, cursor: fresh.length ? fresh[fresh.length - 1].seq : cursor });
      }
      setTimeout(poll, 300);
    };
    return poll();
  }

  if (p === "/api/v1/widget/uploads" && req.method === "POST") {
    req.resume(); // drain multipart body
    req.on("end", () =>
      json(res, {
        file_id: "f-" + Math.random().toString(36).slice(2),
        url: "https://placehold.co/300x200",
        mime: "image/png",
        size: 1234,
        name: "upload.png",
      }),
    );
    return;
  }

  if (["/api/v1/widget/lead", "/api/v1/widget/track", "/api/v1/widget/identify"].includes(p)) {
    await readBody(req);
    return json(res, { ok: true });
  }

  res.writeHead(404);
  res.end("not found");
}).listen(PORT, () => {
  console.log(`demo: http://localhost:${PORT}/host.html`);
});
