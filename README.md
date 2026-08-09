# AURTOR LINE Bot

部署於 Render 的 LINE Messaging API Webhook。

## 功能

- 使用 LINE Channel Secret 驗證 Webhook 簽章
- 三位使用者共用同一組 Webhook
- 收到 `My ID` 時，回覆該傳送者自己的 LINE User ID
- 內部員工在個人聊天室以一句口語描述代墊內容，Bot 自動解析完整性並用一張圖卡確認
- 高爾賢個人聊天室的報價方案、草稿修改與明確送出事件會轉交報價後端
- 內部員工可輸入 `外案 8月10號 1萬`，Bot 會補問缺少欄位；主管核准後才寫入獎金試算表
- 高爾賢、阿筌與周暐可輸入 `今日行程`、`明天的行程`、`後天行程`、`這週的行程` 等關鍵字，取得對應日期範圍的藍色 Google Calendar 圖卡；單獨輸入 `行程` 維持回傳今日與明日

代墊範例：

```text
代墊昨天 TOYOTA 拍攝餐費 850 元，我先付的，沒收據
```

資料不足時，Bot 會一次列出所有缺漏欄位，不會重新逐題詢問。

也可直接上傳發票或收據照片，再輸入 `代墊`。Bot 會辨識日期、商家、品項、總額與統編狀態，並用確認圖卡完成登記。
- 提供 `/health` 健康檢查

## Render 環境變數

請在 Render Dashboard 設定，禁止將真實值提交到 GitHub：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `GROUP_REGISTRY_URL`
- `GROUP_REGISTRY_API_KEY`
- `EXPENSE_API_URL`（Google Apps Script 支出寫入端點）
- `GEMINI_API_KEY`（收據影像辨識，只放 Render Secret）
- `PROJECT_API_URL`、`PROJECT_API_KEY`（選填；近期未結案專案來源，未設定時使用手動輸入）
- `BONUS_API_URL`、`BONUS_API_KEY`（外案待核准紀錄與獎金試算表寫入端點）
- `QUOTE_WEBHOOK_URL`（報價後端 Webhook，預設 `https://linebot-bam2.onrender.com/webhook`）
- `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REFRESH_TOKEN`（Google Calendar OAuth；refresh token 必須包含 `calendar.readonly` scope）
- `CALENDAR_IDS`（固定合併 `contact@goalbrother.com,aurtorfilm@gmail.com`）

員工確認送出後，Bot 會把 LINE 原始收據 Base64、檔名與交易識別碼交給 Apps Script；Apps Script 負責存入公司 Google Drive，並把附件連結與代墊資料寫入 Google Form 連動的回覆試算表。相同交易識別碼重試時應更新原紀錄，不重複建立附件或資料列。

代墊流程會累積員工每次補充的欄位，不會清空既有收據。消費項目固定為交通、餐飲、道具、場景、器材、演員、服裝、其他工作人員及後期；無法自動判定時以 LINE 圖卡讓員工點選。缺少專案、項目或金額時均提供完成方式與取消按鈕。
- `EXPENSE_API_KEY`
- `GEMINI_API_KEY`（收據影像辨識）
- `RECEIPT_VISION_MODEL`（預設 `gemini-3.6-flash`）

## Webhook

部署完成後，在 LINE Developers Console 設定：

```text
https://<Render 服務網址>/webhook
```
