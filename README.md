# AURTOR LINE Bot

部署於 Render 的 LINE Messaging API Webhook。

## 功能

- 使用 LINE Channel Secret 驗證 Webhook 簽章
- 三位使用者共用同一組 Webhook
- 收到 `My ID` 時，回覆該傳送者自己的 LINE User ID
- 內部員工在個人聊天室輸入 `代墊`，以圖卡及對話完成支出登記
- 提供 `/health` 健康檢查

## Render 環境變數

請在 Render Dashboard 設定，禁止將真實值提交到 GitHub：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`
- `GROUP_REGISTRY_URL`
- `GROUP_REGISTRY_API_KEY`
- `EXPENSE_API_URL`（Google Apps Script 支出寫入端點）
- `EXPENSE_API_KEY`

## Webhook

部署完成後，在 LINE Developers Console 設定：

```text
https://<Render 服務網址>/webhook
```
