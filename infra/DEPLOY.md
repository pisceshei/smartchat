# SmartChat 生產部署手冊（寶塔伺服器 183.178.215.103 → chat.chilling.com.hk）

決策：**直接切 chat.chilling.com.hk**、自用帳號**預設 Max**。

## 0. 前置
- 伺服器容量已核實：466G 碟餘 348G、31G 記憶體餘 23G，充足。
- 現役舊系統：Chatwoot 4 容器 + connector（`/root/chilling-chat`），切換時備份後停。

## 1. 代碼上伺服器（擇一）
- A. GitHub：建私有 repo `pisceshei/smartchat` → 本機 `git push` → 伺服器 `git clone` 到 `/root/smartchat`
- B. 上傳 bundle：本機 `git bundle create smartchat.bundle --all` → 寶塔文件管理上傳到 `/root/` → 伺服器 `git clone smartchat.bundle smartchat`

## 2. 備份舊 Chatwoot（切換前必做）
```bash
mkdir -p /root/_backup_chatwoot_$(date +%F)
cd /root/chilling-chat && docker compose ... exec -T postgres pg_dumpall -U postgres > /root/_backup_chatwoot_$(date +%F)/pg.sql
tar czf /root/_backup_chatwoot_$(date +%F)/chilling-chat.tgz /root/chilling-chat
```

## 3. 配置
```bash
cd /root/smartchat && cp infra/.env.prod.example .env
# 填 POSTGRES_PASSWORD / SECRET_KEY / CREDENTIALS_MASTER_KEY(永不改) / MINIO_ROOT_PASSWORD / BRIDGE_API_TOKEN
# 生成 master key: docker run --rm python:3.12-slim python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

## 4. 構建 + 起棧
```bash
cd /root/smartchat
docker compose -f infra/docker-compose.yml build          # Python + Go bridge + web + embed(首次拉 bge-m3 ~2GB)
docker compose -f infra/docker-compose.yml up -d postgres redis minio embed
# 遷移 + 種子
docker compose -f infra/docker-compose.yml run --rm api alembic -c apps/api/alembic/alembic.ini upgrade head
docker compose -f infra/docker-compose.yml run --rm api python -m apps.api.app.seed
docker compose -f infra/docker-compose.yml up -d           # 全部服務
```

## 5. Nginx 反代（寶塔 網站 → chat.chilling.com.hk）
- `/`            → web-nginx（SPA + widget 資產）
- `/api/`        → api:8000
- `/ws/`         → ws-gateway:8001（WebSocket upgrade）
- `/widget/`     → ws-gateway:8001（long-poll）
- `/hooks/`      → api:8000（渠道 webhook）
- `/s/`          → edge:8002（分流短鏈）
- 複用現有 CF Origin 泛域證書；proxy_read_timeout 90s；client_max_body_size 25m

## 6. 自用帳號設 Max
```bash
# 註冊 chilling 自用帳號後，用 super_admin 呼叫 /api/v1/billing/admin/change-plan {plan_code:"max", duration_days:720}
```

## 7. 切換 + 下線舊系統
- Nginx 站點指向新系統 → reload
- 觀察新系統正常後：`cd /root/chilling-chat && docker compose down`（保留數據卷 30 天）
- fecify 店鋪模板中的舊 widget 腳本換成新 embed `/js/project_{key}.js`

## 8. 回滾（72h 內）
- Nginx 反代改回舊 Chatwoot 容器（數據卷未刪即分鐘級恢復）

## 驗收清單
- [ ] 登入後台、收件匣三欄
- [ ] widget 嵌入 + 訪客收發
- [ ] 接一個真實渠道（Telegram token / WhatsApp 掃碼）
- [ ] WhatsApp App 掃碼 → online
- [ ] 後台輸入 Stripe key → 訂閱結帳
- [ ] 群發 / 報表 / 自動化流程
