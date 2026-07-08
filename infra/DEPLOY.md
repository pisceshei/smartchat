# SmartChat 生產部署手冊（寶塔伺服器 183.178.215.103 → chat.chilling.com.hk）

決策：**直接切 chat.chilling.com.hk**、自用帳號**預設 Max**。
狀態：**已部署上線 2026-07-08**（12 容器全綠、舊 Chatwoot 已停、CF→origin 正常）。

## ⚠️ 最關鍵操作紀律：所有 compose 指令都要 `--env-file .env`
`docker compose -f infra/docker-compose.yml` 會把「專案目錄」定為 `infra/`，於是插值變數
（`${POSTGRES_PASSWORD}` / `${MINIO_ROOT_PASSWORD}`）去找 `infra/.env`（不存在）→ **回退到預設
`smartchat`**，而 api 的 `env_file: ../.env` 卻讀到真密碼 → postgres/minio 密碼與 app 不符 →
`password authentication failed`。**修正：每個 compose 指令都加 `--env-file .env`（在 /root/smartchat 下跑）。**
```bash
cd /root/smartchat
alias dc='docker compose -f infra/docker-compose.yml --env-file .env'
```
> 只在 **create** 時要緊（容器 env 建立即固化）；已建好的容器重啟/開機自動起不受影響。但任何
> `up`/`run`/`down -v` 重建都必須帶 `--env-file .env`，否則又會回退預設密碼。

## 0. 前置
- 伺服器容量：16 核 / 31G RAM / 466G 碟（餘 348G），充足。
- 舊系統：`/root/chilling-chat`（chatwoot 於 `chatwoot/docker-compose.yaml`+override、connector 於 `connector/docker-compose.yml`）。

## 1. 代碼上伺服器
GitHub：`git clone https://github.com/pisceshei/smartchat.git /root/smartchat`（repo 公開期間免登入）。

## 2. 備份舊 Chatwoot（切換前必做）
```bash
mkdir -p /root/_backup_chatwoot_$(date +%F)
# pg_dumpall 的使用者是 chatwoot 不是 postgres（否則 dump 為 0 byte）
docker exec -t chatwoot-postgres-1 pg_dumpall -U chatwoot > /root/_backup_chatwoot_$(date +%F)/pg.sql
tar czf /root/_backup_chatwoot_$(date +%F)/chilling-chat.tgz /root/chilling-chat
cp /www/server/panel/vhost/nginx/chat.chilling.com.hk.conf /root/_backup_chatwoot_$(date +%F)/chat.nginx.conf.bak
```

## 3. 配置 .env（直接在伺服器寫，勿進 git）
`infra/.env.prod.example` 內含**真實 LLM key**，被 `.gitignore` 的 `.env.*` 擋住（正確：勿把 key 推上
公開 repo）。伺服器上用 heredoc 直接生成 `.env`（openssl 現場生密鑰、sub2api 值已知）：
```bash
cd /root/smartchat
SK=$(openssl rand -hex 32); MK=$(openssl rand -base64 32 | tr '+/' '-_')
DBP=$(openssl rand -hex 16); MNP=$(openssl rand -hex 16); BRT=$(openssl rand -hex 24)
cat > .env <<EOF
DATABASE_URL=postgresql+asyncpg://smartchat:${DBP}@postgres:5432/smartchat
POSTGRES_PASSWORD=${DBP}
REDIS_URL=redis://redis:6379/0
SECRET_KEY=${SK}
CREDENTIALS_MASTER_KEY=${MK}          # 永不更改，否則所有加密渠道憑證失效
PUBLIC_BASE_URL=https://chat.chilling.com.hk
ASSETS_BASE_URL=https://chat.chilling.com.hk
MINIO_ENDPOINT=minio:9000
MINIO_ROOT_USER=smartchat
MINIO_ROOT_PASSWORD=${MNP}
MINIO_BUCKET=smartchat
MINIO_SECURE=false
LLM_PROVIDER=anthropic
LLM_BASE_URL=https://sub2api.chilling.com.hk
LLM_API_KEY=<sub2api key>
LLM_MODEL_FAST=claude-haiku-4-5-20251001
LLM_MODEL_SMART=claude-sonnet-4-6
LLM_MODEL_EMBED=bge-m3
EMBED_BASE_URL=http://embed:8090
EMBED_DIM=1024
BRIDGE_WA_URL=http://bridge-wa:8100
BRIDGE_API_TOKEN=${BRT}
BRIDGE_STORE_DIR=/data
BRIDGE_PUBLIC_URL=http://bridge-wa:8100
SMARTCHAT_FILES_BASE=https://chat.chilling.com.hk
STRIPE_SECRET_KEY=
STRIPE_PUBLISHABLE_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_CURRENCY=usd
EOF
```
> Stripe key、Telegram token、各渠道憑證 **部署後在後台輸入**（Stripe 走託管 Checkout，
> 前端不需 build-time publishable key）。

## 4. 構建 + 起棧（全部帶 --env-file）
```bash
dc build            # widget(node)→api鏡像 / web(node+nginx) / bridge-wa(Go) / embed(torch2.6+bge-m3 ~6GB)
dc up -d postgres redis minio
dc run --rm api alembic -c apps/api/alembic/alembic.ini upgrade head   # 0001→0005
dc run --rm api python -m apps.api.app.seed                            # 4 plans
dc up -d            # 全部 12 服務
dc ps               # 確認全 Up；postgres+bridge-wa healthy
```
構建注意：① 前端 dist 皆 gitignored，靠 Docker node 階段建（backend.Dockerfile 建 widget、web.Dockerfile 建 SPA）
② embed 需 `torch>=2.6`（transformers CVE-2025-32434 擋 <2.6 的 torch.load）
③ 若 postgres/minio 曾用預設密碼建過，`dc down -v` 清卷後帶 `--env-file` 重建。

## 5. Nginx 反代（寶塔站點 chat.chilling.com.hk.conf；參考 infra/nginx/site-chat.conf）
容器皆綁 127.0.0.1：`/`→web:8080、`/api/`+`/hooks/`+`/js/`+`/widget-app`→api:8000、
`/ws/`（WS upgrade，含 `$connection_upgrade`）+`/widget/`（long-poll 30s）→ws-gateway:8001、`/s/`→edge:8002。
複用現有 CF Origin 泛域證書；`nginx -t && nginx -s reload`。origin 直測：`curl -sk --resolve chat.chilling.com.hk:443:127.0.0.1 https://chat.chilling.com.hk/`。

## 6. 自用帳號設 Max（帳號註冊由用戶自行完成，其餘我方 provisioning）
1. 用戶在 https://chat.chilling.com.hk 註冊（自訂 email+密碼）→ 首個用戶自動成為該工作區 super_admin（free 方案）。
2. 一鍵升 Max（無收費，等同已付 webhook 效果）：
```bash
dc run --rm -v /root/smartchat/apps/api/app/set_plan.py:/srv/smartchat/apps/api/app/set_plan.py \
  api python -m apps.api.app.set_plan <你的email> max 720
# 鏡像已含 set_plan.py 後可簡化：dc run --rm api python -m apps.api.app.set_plan <email> max 720
```

## 7. 切換 + 下線舊系統（已完成）
- Nginx 站點指向新系統 → reload ✓
- 停舊：`cd /root/chilling-chat/chatwoot && docker compose -f docker-compose.yaml -f docker-compose.override.yaml down`；`cd ../connector && docker compose down`（保留數據卷 30 天）✓
- fecify 店鋪模板中的舊 widget 腳本換成新 embed `/js/project_{key}.js`（待辦）

## 8. 回滾（72h 內）
- 還原 nginx 設定：`cp /root/_backup_chatwoot_*/chat.nginx.conf.bak <conf> && nginx -s reload`
- 重起舊容器：進 chatwoot/connector 目錄 `docker compose up -d`（數據卷未刪即分鐘級恢復）

## 驗收清單（2026-07-08）
- [x] 12 容器全 Up；postgres+bridge-wa healthy；DB 0001→0005；4 plans
- [x] 登入頁 SPA（chat.chilling.com.hk 經 CF）
- [x] widget loader `/js/project_*.js` 200；widget iframe `/widget-app/` 200
- [x] API 反代（`/api/v1/*` 受 workspace scope 保護）；edge `/s/` 路由
- [x] embed 側車產 1024 維向量（RAG 就緒）
- [x] bridge-wa healthy（WhatsApp App 掃碼橋）
- [ ] 用戶註冊自用帳號 → `set_plan` 升 Max（待用戶註冊）
- [ ] 後台輸入 Telegram token / Stripe key → 真渠道收發 + 訂閱結帳（待用戶輸入憑證）
- [ ] 收件匣三欄 / 群發 / 報表 / 流程（登入後；本機 E2E 16/16 已過）
