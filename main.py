"""AURTOR LINE Bot：共用 Webhook、內部 ID 查詢與客戶群組辨識。"""

import base64
import hashlib
import hmac
import os
from typing import Any

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_REGISTRY_URL = os.environ.get("GROUP_REGISTRY_URL", "").rstrip("/")
GROUP_REGISTRY_API_KEY = os.environ.get("GROUP_REGISTRY_API_KEY", "")
OWNER_USER_ID = os.environ.get(
    "OWNER_USER_ID",
    "U6c6441cb38102499d1f80d4ea79a53ab",
)
DEFAULT_INTERNAL_USER_IDS = (
    "U6c6441cb38102499d1f80d4ea79a53ab,"
    "Ub983deb79584603885e5b28e9fdf2d5d,"
    "U9478b00702c716685d9d8b021d62d538"
)
INTERNAL_USER_IDS = {
    user_id.strip()
    for user_id in os.environ.get(
        "INTERNAL_USER_IDS",
        DEFAULT_INTERNAL_USER_IDS,
    ).split(",")
    if user_id.strip()
}

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


def line_headers() -> dict[str, str]:
    """建立 LINE Messaging API 共用標頭。"""
    return {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def reply_text(reply_token: str, text: str) -> None:
    """使用事件的 Reply Token 回覆文字訊息。"""
    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=line_headers(),
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=10,
    )
    response.raise_for_status()


def get_group_name(group_id: str) -> str:
    """依照 Group ID 向 LINE 自動取得目前群組名稱。"""
    response = requests.get(
        f"https://api.line.me/v2/bot/group/{group_id}/summary",
        headers=line_headers(),
        timeout=10,
    )
    response.raise_for_status()
    return response.json().get("groupName", "（無法取得群組名稱）")


def get_client_record(group_id: str) -> dict[str, Any] | None:
    """從共用 Google Sheet API 查詢此群組所屬客戶。"""
    if not GROUP_REGISTRY_URL or not GROUP_REGISTRY_API_KEY:
        return None

    response = requests.get(
        GROUP_REGISTRY_URL,
        params={"key": GROUP_REGISTRY_API_KEY, "groupId": group_id},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    record = payload.get("record")
    return record if payload.get("ok") and isinstance(record, dict) else None


def build_group_reply(source: dict[str, Any]) -> str:
    """組合 Group ID、LINE 群組名稱與已綁定客戶。"""
    if source.get("type") != "group":
        return "這則訊息不是從 LINE 群組送出，因此沒有 Group ID。"

    group_id = source.get("groupId", "")
    if not group_id:
        return "目前無法取得 Group ID。"

    group_name = get_group_name(group_id)
    lines = [
        f"Group ID：{group_id}",
        f"群組名稱：{group_name}",
    ]

    record = get_client_record(group_id)
    if record:
        lines.append(f"客戶名稱：{record.get('clientName', '（未設定）')}")
    else:
        lines.append("客戶名稱：尚未綁定")

    return "\n".join(lines)


@app.post("/webhook")
async def webhook(request: Request):
    """接收 LINE Webhook，並處理文字指令。"""
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    payload = await request.json()

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        command = message.get("text", "").strip().casefold()
        source = event.get("source", {})
        user_id = source.get("userId", "")

        if command == "my id":
            result = f"User ID：{user_id}" if user_id else "目前無法取得 User ID。"
        elif command in {"group id", "group_id", "groupid"}:
            # Group ID 是內部資料，只讓已登記的三位夥伴查詢。
            if user_id not in INTERNAL_USER_IDS:
                continue
            try:
                result = build_group_reply(source)
            except requests.RequestException:
                result = "目前無法取得群組資料，請稍後再試。"
        else:
            continue

        try:
            reply_text(event.get("replyToken", ""), result)
        except requests.RequestException:
            return JSONResponse({"error": "LINE reply failed"}, status_code=502)

    return {"status": "ok"}


@app.get("/health")
async def health():
    """供 Render 確認服務與必要環境變數是否正常。"""
    return {
        "status": "ok",
        "line_secret_configured": bool(LINE_CHANNEL_SECRET),
        "line_token_configured": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "registry_configured": bool(
            GROUP_REGISTRY_URL and GROUP_REGISTRY_API_KEY
        ),
        "internal_user_count": len(INTERNAL_USER_IDS),
        "owner_configured": bool(OWNER_USER_ID),
    }


@app.get("/")
async def root():
    """提供部署狀態說明，不顯示任何敏感憑證。"""
    return {"service": "AURTOR LINE Bot", "status": "running"}
