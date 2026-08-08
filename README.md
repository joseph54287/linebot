# AURTOR LINE Bot

部署於 Render 的 LINE Messaging API Webhook。

## 功能

- 使用 LINE Channel Secret 驗證 Webhook 簽章
- 三位使用者共用同一組 Webhook
- 收到 `My ID` 時，回覆該傳送者自己的 LINE User ID
- 內部員工在個人聊天室以一句口語描述代墊內容，Bot 自動解析完整性並用一張圖卡確認

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
- `EXPENSE_API_KEY`
- `GEMINI_API_KEY`（收據影像辨識）
- `RECEIPT_VISION_MODEL`（預設 `gemini-3.6-flash`）

## Webhook

部署完成後，在 LINE Developers Console 設定：

```text
https://<Render 服務網址>/webhook
```
