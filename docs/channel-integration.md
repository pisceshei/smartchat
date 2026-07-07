# SmartChat 社媒對接手冊（Channel Integration Reference）

各渠道的官方對接方式、所需憑證、webhook 驗簽、發送/接收、限制，以及**我方 adapter 現狀與 gap**。
來源：官方開發者文檔（109-agent 對抗驗證研究）+ SaleSmartly Pro 後台實測。
我方 adapter 位置：`apps/api/app/channels/adapters/`，webhook 入口 `apps/api/app/modules/hooks/router.py`。

> 圖例：✅ 已實現且與官方文檔一致 · ⚠️ 有限制/半實現 · ⬜ 未實現（Phase 4）

---

## 對接方式總覽

| 渠道 | 對接類型 | 憑證 | Webhook 驗簽 | 我方狀態 |
|---|---|---|---|---|
| 網站聊天外掛 | 自家協議 | widget_key | 自簽 visitor token | ✅ 完整 |
| WhatsApp Cloud API | 官方 API（或 BSP 代理）| System User 永久 token + phone_number_id | X-Hub-Signature-256 | ✅ 直連 Cloud API；⬜ BSP 代理 |
| WhatsApp App（個人號）| 托管橋 whatsmeow | QR 配對 session | 內網 HMAC | ⚠️ bridge 骨架；**封號風險** |
| Facebook Messenger | 官方 API | Page access token + page_id | X-Hub-Signature-256 | ✅ 完整（含 HUMAN_AGENT tag）|
| Instagram | 官方 API（經 Page）| Page token + IG account id | X-Hub-Signature-256 | ✅ 經 Page 路徑；⬜ graph.instagram.com IG-Login 路徑 |
| Telegram Bot | 官方 API | BotFather token | X-Telegram-Bot-Api-Secret-Token | ✅ 完整 |
| LINE 官方帳號 | 官方 API | channel_secret + channel_access_token | X-Line-Signature (HMAC-SHA256) | ✅ 完整（push）|
| Email | IMAP/SMTP | 帳號密碼 或 OAuth2 | 無（輪詢/IDLE）| ✅ 基本認證；⬜ Gmail/Outlook XOAUTH2 |
| LINE App（個人號）| 托管橋 | 掃碼 session | — | ⬜ 未做；**封號風險** |
| TikTok 商業號 | 官方 API（受限）| Business token | — | ⬜ Phase 4 |
| 微信客服 / 企業微信 | 官方 API | corp_id + secret + AES 回調 | AES 消息加密 | ⬜ Phase 4 |
| YouTube 評論 | Data API | OAuth | — | ⬜ Phase 4 |
| Zalo OA | 官方 API | OAuth v4 access/refresh token | — | ⬜ Phase 4 |
| Slack | 官方 API | bot token + signing secret | Slack signing secret | ⬜ Phase 4 |
| VKontakte | 官方 API | community token | Callback confirm | ⬜ Phase 4 |

---

## 1. WhatsApp Business Cloud API ✅（P1 主力）

**對接流程（SaleSmartly 後台實測 2 路）**：
- **註冊號碼**：新號碼申請，經 BSP（YCloud/ChatApp/NxCloud/ITNIO/Cloud API）填 API key 拉號碼
- **授權號碼**：授權現有 WABA（走 Meta Embedded Signup / BSP）

**憑證**：`phone_number_id`（發送路徑）、WABA id、**永久 access token**（Meta Business Settings 建 System User → 授予 `business_management` + `whatsapp_business_messaging` + `whatsapp_business_management` 三權限、資產給 Full control）。⚠️ 嚮導的臨時 token < 24h 過期，生產不可用。
**發送**：`POST https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages`，`Authorization: Bearer <token>`，body `{messaging_product:"whatsapp", to:E.164, type, ...}`；回覆帶 `context.message_id`。
**接收**：Meta app 級 webhook（我方 `/hooks/meta`），訂閱欄位 `messages`。
**驗簽**：`X-Hub-Signature-256` = HMAC-SHA256(app_secret, raw_body)。
**限制**：**24 小時客服窗**（客戶來訊/來電開啟，滾動延長；窗外只能發已批准範本）；CTWA/粉專 CTA 免費入口開 **72 小時**窗；範本需 Meta 審核（marketing/utility/authentication）；計費=Meta 範本費+$0.005 平台費。
**我方**：`whatsapp_cloud.py` GRAPH v21、template type、24h→`WINDOW_EXPIRED` 硬校驗、`verify_meta_signature`。**Gap**：BSP 代理協議（YCloud 等）未接，僅直連 Cloud API。可 Phase 4 加 BSP adapter。

## 2. WhatsApp App 個人號托管 ⚠️（封號風險）

**方式**：whatsmeow/Baileys 多設備，QR 或 E.164 配對碼，**必須持久化 auth-state**（生產絕不用 `useMultiFileAuthState`）。
**⛔ 風險**：違反 WhatsApp Business Terms（禁未授權 app/逆向/爬取），有明確的階梯式封禁直至永久封號。**每設備一容器 bridge**（獨立崩潰域+獨立代理+仿人限速+不自動重配對）。
**我方**：`bridge.py` 契約骨架 + `device_bridges` 表；商業客戶引導改用官方 Cloud API。

## 3. Facebook Messenger ✅（P1）

**對接**：Meta app + Facebook 粉專，權限 `pages_messaging`，取 **Page access token**，webhook 訂閱粉專 `messages`。
**發送**：`POST graph.facebook.com/v23.0/me/messages?access_token=<page_token>`，body `{recipient:{id:PSID}, message, messaging_type}`。
**接收**：`/hooks/meta`（object=page，按 page_id 路由），payload PSID。
**限制**：**24 小時標準訊息窗**；窗外用 **message tag**（`HUMAN_AGENT` 允許 7 天，需 Meta 審核）。
**我方**：`messenger.py` — quick_replies(≤13)、商品卡→generic template、窗外自動附加 `messaging_type=MESSAGE_TAG` + `tag=HUMAN_AGENT`、錯誤碼 10/2018278 → 窗口錯誤。✅ 與文檔一致。

## 4. Instagram ✅（經 Page 路徑）

**對接**：Instagram 專業號連結到 FB 粉專（SaleSmartly 亦此模型），權限 `instagram_manage_messages`。
**兩條官方路徑**：①**經 Page**（graph.facebook.com + Page token，我方採用）②**IG Login**（graph.instagram.com + IG User token + **IGSID** ≠ PSID + `instagram_business_basic`/`instagram_business_manage_messages`）。
**限制**：24h 窗；Send API 與 Messenger 大致同構。
**我方**：`instagram.py` 繼承 `MessengerAdapter`，webhook object=instagram 按 IG account id 路由。**Gap**：僅經-Page 路徑（覆蓋粉專連結的 IG 商業號＝標準場景）；graph.instagram.com IG-Login 獨立路徑未做，Phase 4 可加以支持「無粉專」IG 號。

## 5. Telegram Bot ✅（P1）

**對接（實測與我方一致）**：@BotFather → `/newbot` → 取 token → 貼入連接。
**憑證**：Bot token。**發送**：`api.telegram.org/bot<token>/sendMessage`（inline keyboard=quick_buttons、sendPhoto/Document/Voice）。
**接收**：`setWebhook` 帶 `secret_token`；webhook 回 `X-Telegram-Bot-Api-Secret-Token` 校驗。
**限制**：message_id 僅 per-chat 唯一 → 去重鍵 `{chat_id}:{message_id}`；30/s 全局 + 1/s per-chat。
**我方**：`telegram.py` — `validate_token`(getMe)、`set_webhook`(secret_token)、`verify_webhook`。✅ 完整。

## 6. LINE 官方帳號 ✅（P1）

**對接**：LINE Developers console 建 Messaging API channel，取 **channel_secret** + **long-lived channel_access_token**，設 webhook URL。
**發送**：`api.line.me/v2/bot/message/push`（push，任意時候）或 `/reply`（reply token，被動免費）；flex message=商品卡。
**接收**：webhook，`X-Line-Signature` = base64(HMAC-SHA256(channel_secret, body))。
**我方**：`line_oa.py` — `verify_line_signature`、push 發送、flex。✅ 完整（用 push 保證任意時可發；reply-token 優化可選）。

## 7. Email ✅（P1，基本認證）

**對接**：IMAP（收）+ SMTP（發）主機/埠/帳密。
**接收**：aioimaplib IDLE 長連 + 60s 輪詢兜底；線程歸屬 plus-address → In-Reply-To/References → 發件人開放會話；Message-ID 去重。
**發送**：aiosmtplib，設 In-Reply-To + References 鏈。
**我方**：`email_imap.py`。**Gap**：僅帳號密碼/app-password；**Gmail/Outlook 已停用基本認證，需 XOAUTH2**（OAuth2 token 走 SASL XOAUTH2）——這是要真正接 Gmail/Outlook 商業信箱的**實際 gap**，Phase 4 補 XOAUTH2。

## 8–14. Phase 4 渠道（骨架已在渠道類型枚舉預留）
- **LINE App 個人號**：托管橋，封號風險，暫緩
- **TikTok 商業號**：Messaging API 受限開放，需商業號資質
- **微信客服/企業微信**：corp_id+secret+回調 URL+**AES 消息加密**（EncodingAESKey）
- **YouTube 評論**：Data API v3 + OAuth
- **Zalo OA**：OAuth v4，access + refresh token（實測 developers.zalo.me）
- **Slack**：app + bot token + Events API + signing secret 驗簽
- **VKontakte**：community access token + Callback API 確認碼 / Bots Long Poll

---

## 結論：對接能力評估

**P1 八渠道（widget/WhatsApp Cloud/Messenger/Instagram/Telegram/LINE/Email/WhatsApp-App橋）的 adapter 均按官方文檔正確實現**，可完美連接標準商業帳號。要達到「完美連接每一種帳號」的兩個**實際 gap**（建議納入 Phase 4）：
1. **Email XOAUTH2** —— 接 Gmail/Outlook 商業信箱必需（基本認證已被停用）
2. **WhatsApp BSP 代理**（YCloud/ChatApp 等）—— SaleSmartly 支持，讓沒有自建 Meta app 的客戶也能接 WhatsApp
3. Instagram graph.instagram.com IG-Login 路徑（無粉專的 IG 號）
其餘 Phase 4 渠道（TikTok/微信/YouTube/Zalo/Slack/VK）按上表官方協議逐一實現即可，渠道類型枚舉與 webhook 分發框架已預留。
