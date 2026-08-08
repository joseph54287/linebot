"""AURTOR LINE Bot：提供共用 Webhook 與 My ID 指令。"""

import base64
import hashlib
import hmac
import os

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

app = FastAPI(title="AURTOR LINE Bot")


def verify_signature(body: bytes, signature: str) -> bool:
    """驗證請求是否確實來自 LINE 平台。"""
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_text(reply_token: str, text: str) -> None:
    """使用事件的 Reply Token 回覆文字訊息。"""
    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )
    response.raise_for_status()


@app.post("/webhook")
async def webhook(request: Request):
    """接收 LINE Webhook，並處理每一筆事件。"""
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        return JSONResponse(
            {"error": "invalid signature"},
            status_code=400,
        )

    payload = await request.json()

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        # 忽略大小寫與前後空白，方便三位使用者直接查詢自己的 ID。
        if message.get("text", "").strip().casefold() != "my id":
            continue

        user_id = event.get("source", {}).get("userId", "")
        result = f"User ID：{user_id}" if user_id else "目前無法取得 User ID。"

        try:
            reply_text(event.get("replyToken", ""), result)
        except requests.RequestException:
            # 不把 Token、User ID 或 LINE 回傳內容寫進公開錯誤訊息。
            return JSONResponse(
                {"error": "LINE reply failed"},
                status_code=502,
            )

    return {"status": "ok"}


@app.get("/health")
async def health():
    """供 Render 確認服務是否正常運作。"""
    return {
        "status": "ok",
        "line_secret_configured": bool(LINE_CHANNEL_SECRET),
        "line_token_configured": bool(LINE_CHANNEL_ACCESS_TOKEN),
    }


@app.get("/")
async def root():
    """提供部署狀態說明，不顯示任何敏感憑證。"""
    return {"service": "AURTOR LINE Bot", "status": "running"}
