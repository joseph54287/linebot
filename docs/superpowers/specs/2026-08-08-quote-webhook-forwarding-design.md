# LINE 報價事件轉送設計

## 目標

在既有 `aurtor-line-bot` 的 `/webhook` 中加入報價事件轉送，維持同一個 LINE Webhook URL，並完整保留三人共用、My ID、群組資料、代墊登記、收據辨識與既有 `expense:*` postback。

## 範圍與安全邊界

- 僅接受 LINE 簽章驗證通過的請求。
- 僅轉送高爾賢個人聊天室事件；LINE User ID 為 `Ub983deb79584603885e5b28e9fdf2d5d`。
- 可轉送事件只有：
  - `action=scheme&invitation=<invitationId>&scheme=A|B|C` 格式的方案 postback。
  - 完全等於 `確認` 或 `送出` 的文字訊息。
  - 去除前後空白後，以 `主旨：` 開頭的完整修改稿。
- 不轉送群組事件、代墊事件、My ID、圖片或其他既有事件。
- 報價後端負責草稿狀態與 Gmail 寄送；本 Bot 不直接呼叫 Gmail API。
- 方案選擇與完整修改稿只更新草稿；只有明確的 `確認` 或 `送出` 才可能由報價後端寄信。

## 設定

新增環境變數：

```text
QUOTE_WEBHOOK_URL=https://linebot-bam2.onrender.com/webhook
```

程式以此網址為預設值，讓既有部署可立即使用；仍允許環境變數覆寫，方便測試及未來搬遷。此整合不新增 API Token，也不更換 LINE Developers Console 中的 Webhook URL。

## 事件判斷

新增純函式 `is_quote_event(event)`，依序確認：

1. `source.type` 必須為 `user`。
2. `source.userId` 必須為高爾賢的 User ID。
3. postback 必須符合方案事件格式，且 `scheme` 僅能為 `A`、`B`、`C`，不可只靠寬鬆前綴誤收其他 action。
4. 文字事件必須為 `確認`、`送出`，或以全形冒號的 `主旨：` 開頭。

## 資料流

1. `/webhook` 先讀取原始 bytes 與 `X-Line-Signature`。
2. 沿用現有 `verify_signature` 驗證 LINE 簽章；失敗仍回傳 `401`。
3. 解析 JSON，判斷整批 `events` 是否至少包含一筆報價事件。
4. 若包含報價事件，將整份原始 bytes 原封不動 POST 至 `QUOTE_WEBHOOK_URL`，並保留：
   - `Content-Type: application/json`
   - 原始 `X-Line-Signature`
5. 不重新序列化 JSON，避免簽章失效。
6. 轉送成功後，現有事件迴圈仍繼續執行；遇到報價事件時直接略過，其他事件照原流程處理。這可避免同批內的代墊、My ID 或群組事件遺失，也避免兩個後端重複使用同一個 Reply Token。
7. 報價後端必須忽略同批原始 body 中不屬於報價流程的事件。

## 錯誤處理

- 轉送逾時、網路錯誤或非 2xx 回應時，記錄不含敏感內容的錯誤資訊。
- 原 Bot 不自行重送報價請求，並仍向 LINE 回傳成功，避免 LINE Webhook redelivery 對 `確認`／`送出` 再次觸發寄信。代價是單次失敗需由使用者重新操作，安全性優先於自動補送。
- 混合批次即使報價轉送失敗，仍處理本地非報價事件，避免代墊、My ID 或群組功能受到報價後端可用性影響。
- 報價後端仍應以 LINE event ID 或自身寄送狀態實作冪等，作為避免重複寄信的最後一道保護。
- 不在 log 中輸出完整原始 body、LINE 簽章或訊息全文。

## 測試

新增單元測試驗證：

- 高爾賢個聊的 A／B／C 方案 postback 會被辨識。
- 其他使用者、群組、其他 postback 不會被辨識。
- `確認`、`送出`、`主旨：...` 會被辨識；圖片及一般文字不會。
- 轉送使用原始 body 與原始簽章，且只呼叫報價後端一次。
- 純既有事件不呼叫報價後端。
- 混合批次在轉送成功後，報價事件由原流程略過，非報價事件仍被處理。
- 報價後端逾時或非 2xx 時，原 Bot 不進行第二次轉送，仍回傳成功並繼續處理非報價事件。
- 既有代墊測試全部通過。

## 驗收標準

- 正式 LINE Webhook URL 不變。
- `QUOTE_WEBHOOK_URL` 可由 Render 環境變數設定。
- 報價後端收到未改動的原始 request body 與簽章。
- 只有指定使用者與指定報價事件觸發轉送。
- 混合批次不會因提前 `return` 而漏掉既有事件。
- 報價事件不會在兩個後端重複回覆。
- 所有新增與既有自動測試通過。
