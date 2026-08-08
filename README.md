# AURTOR LINE Bot

部署於 Render 的 LINE Messaging API Webhook。

## 功能

- 使用 LINE Channel Secret 驗證 Webhook 簽章
- 三位使用者共用同一組 Webhook
- 收到 `My ID` 時，回覆該傳送者自己的 LINE User ID
- 提供 `/health` 健康檢查

## Render 環境變數

請在 Render Dashboard 設定，禁止將真實值提交到 GitHub：

- `LINE_CHANNEL_SECRET`
- `LINE_CHANNEL_ACCESS_TOKEN`

## Webhook

部署完成後，在 LINE Developers Console 設定：

```text
https://<Render 服務網址>/webhook
```
