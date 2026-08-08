"""AURTOR LINE Bot：共用 Webhook、內部 ID 查詢與客戶群組辨識。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_REGISTRY_URL = os.environ.get("GROUP_REGISTRY_URL", "").rstrip("/")
GROUP_REGISTRY_API_KEY = os.environ.get("GROUP_REGISTRY_API_KEY", "")
EXPENSE_API_URL = os.environ.get("EXPENSE_API_URL", GROUP_REGISTRY_URL).rstrip("/")
EXPENSE_API_KEY = os.environ.get("EXPENSE_API_KEY", GROUP_REGISTRY_API_KEY)
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

# Render 只暫存尚未送出的對話；完成、取消或逾時後立即清除。
EXPENSE_SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_TTL_SECONDS = 30 * 60

EXPENSE_CATEGORIES = [
    "案件支出（餐飲、道具、人員...）",
    "例行性支出(水電費/房租)",
    "人事費用(商業保險費,薪資 ,勞保,健保,勞退）",
    "工具設備（軟體/硬體）",
    "會計支出(營業稅,營所稅,申報費)",
    "公司雜費",
    "業務開發",
    "行銷費用",
]
EXPENSE_PAYERS = [
    "公司", "公司(未付)", "周暐", "高爾賢", "Moose",
    "Well", "小歐", "Jeffrey", "Marshall",
]
PAYMENT_SCHEDULES = ["立即支付", "月結(每月5號)", "已支出"]
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


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


def reply_messages(reply_token: str, messages: list[dict[str, Any]]) -> None:
    """使用事件的 Reply Token 回覆一或多則 LINE 訊息。"""
    response = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=line_headers(),
        json={"replyToken": reply_token, "messages": messages},
        timeout=10,
    )
    response.raise_for_status()


def reply_text(reply_token: str, text: str) -> None:
    """回覆單一文字訊息。"""
    reply_messages(reply_token, [{"type": "text", "text": text}])


def option_card(title: str, options: list[str], field: str) -> dict[str, Any]:
    """建立可直接點選的支出登記 Flex 圖卡。"""
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "margin": "sm",
            "action": {
                "type": "postback",
                "label": option[:20],
                "data": f"expense:{field}:{option}",
                "displayText": option,
            },
        }
        for option in options
    ]
    return {
        "type": "flex",
        "altText": title,
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#17365D",
                "contents": [{"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold"}],
            },
            "body": {"type": "box", "layout": "vertical", "contents": buttons},
        },
    }


def date_card() -> dict[str, Any]:
    """建立支出日期選擇圖卡。"""
    return {
        "type": "template",
        "altText": "請選擇支出日期",
        "template": {
            "type": "buttons",
            "title": "代墊登記",
            "text": "請選擇支出日期",
            "actions": [
                {"type": "postback", "label": "今天", "data": f"expense:date:{datetime.now(TAIPEI_TZ).date().isoformat()}", "displayText": "今天"},
                {"type": "datetimepicker", "label": "選擇其他日期", "data": "expense:date", "mode": "date"},
                {"type": "postback", "label": "取消登記", "data": "expense:cancel", "displayText": "取消登記"},
            ],
        },
    }


def new_expense_session(user_id: str) -> dict[str, Any]:
    """建立新的代墊登記暫存。"""
    session = {"step": "date", "updated_at": time.time(), "data": {"registrantUserId": user_id}}
    EXPENSE_SESSIONS[user_id] = session
    return session


def get_expense_session(user_id: str) -> dict[str, Any] | None:
    """取得未逾時的代墊登記暫存。"""
    session = EXPENSE_SESSIONS.get(user_id)
    if not session:
        return None
    if time.time() - session["updated_at"] > SESSION_TTL_SECONDS:
        EXPENSE_SESSIONS.pop(user_id, None)
        return None
    session["updated_at"] = time.time()
    return session


def next_prompt(session: dict[str, Any]) -> dict[str, Any]:
    """依目前步驟產生下一個問題。"""
    step = session["step"]
    if step == "project":
        return {"type": "text", "text": "請輸入專案名稱；沒有專案請輸入「無」。"}
    if step == "item":
        return {"type": "text", "text": "請輸入消費項目，例如：拍攝餐費、硬碟、停車費。"}
    if step == "amount":
        return {"type": "text", "text": "請輸入支出金額，只輸入數字即可。"}
    if step == "category":
        return option_card("請選擇項目分類", EXPENSE_CATEGORIES, "category")
    if step == "payer":
        return option_card("請選擇支出人", EXPENSE_PAYERS, "payer")
    if step == "payment":
        return option_card("請選擇付款時程", PAYMENT_SCHEDULES, "payment")
    if step == "reimbursed":
        return option_card("支出人是否已領款？", ["是", "否"], "reimbursed")
    if step == "invoice":
        return option_card("是否開立含統編發票？", ["是", "否", "未開"], "invoice")
    if step == "receipt":
        return option_card("請傳送收據照片", ["略過收據"], "receipt")
    if step == "note":
        return {"type": "text", "text": "請輸入備註或匯款帳戶；沒有請輸入「無」。若未附收據，請說明原因。"}
    return build_expense_confirmation(session["data"])


def build_expense_confirmation(data: dict[str, Any]) -> dict[str, Any]:
    """建立送出前的資料確認圖卡。"""
    summary = "\n".join([
        f"日期：{data.get('date', '')}",
        f"專案：{data.get('project', '')}",
        f"項目：{data.get('item', '')}",
        f"金額：${data.get('amount', '')}",
        f"分類：{data.get('category', '')}",
        f"支出人：{data.get('payer', '')}",
        f"付款：{data.get('payment', '')}",
        f"已領款：{data.get('reimbursed', '')}",
        f"統編發票：{data.get('invoice', '')}",
        f"收據：{'已附照片' if data.get('receiptBase64') else '未附'}",
        f"備註：{data.get('note', '')}",
    ])
    return {
        "type": "flex",
        "altText": "請確認代墊資料",
        "contents": {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": "#17365D", "contents": [{"type": "text", "text": "確認代墊資料", "color": "#FFFFFF", "weight": "bold"}]},
            "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": summary, "wrap": True, "size": "sm"}]},
            "footer": {"type": "box", "layout": "vertical", "contents": [
                {"type": "button", "style": "primary", "action": {"type": "postback", "label": "確認送出", "data": "expense:confirm", "displayText": "確認送出"}},
                {"type": "button", "action": {"type": "postback", "label": "取消登記", "data": "expense:cancel", "displayText": "取消登記"}},
            ]},
        },
    }


def get_line_profile_name(user_id: str) -> str:
    """取得登記人的 LINE 顯示名稱。"""
    response = requests.get(f"https://api.line.me/v2/bot/profile/{user_id}", headers=line_headers(), timeout=10)
    response.raise_for_status()
    return response.json().get("displayName", "LINE 使用者")


def download_line_image(message_id: str) -> tuple[str, str]:
    """下載員工傳送的收據圖片。"""
    response = requests.get(f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}, timeout=20)
    response.raise_for_status()
    mime_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return base64.b64encode(response.content).decode("ascii"), mime_type


def submit_expense(data: dict[str, Any]) -> dict[str, Any]:
    """交由 Google Apps Script 上傳收據並新增支出資料。"""
    if not EXPENSE_API_URL or not EXPENSE_API_KEY:
        raise requests.RequestException("expense api is not configured")
    response = requests.post(EXPENSE_API_URL, params={"key": EXPENSE_API_KEY}, json={"action": "expense", "expense": data}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise requests.RequestException("expense api rejected the update")
    return payload


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


def upsert_client_record(
    group_id: str,
    group_name: str,
    bound_by: str,
) -> dict[str, Any]:
    """將群組名稱同時作為客戶名稱，新增或更新共用 Google Sheet。"""
    if not GROUP_REGISTRY_URL or not GROUP_REGISTRY_API_KEY:
        raise requests.RequestException("group registry is not configured")

    response = requests.post(
        GROUP_REGISTRY_URL,
        params={"key": GROUP_REGISTRY_API_KEY},
        json={
            "clientName": group_name,
            "groupId": group_id,
            "groupName": group_name,
            "boundBy": bound_by,
            "note": "由 LINE 的 ID 指令自動建立",
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise requests.RequestException("group registry rejected the update")
    return payload.get("record", {})


def build_group_reply(source: dict[str, Any], user_id: str) -> str:
    """取得群組資料；周暐查詢時同步自動寫入 Google Sheet。"""
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

    # 只有周暐的指令會自動建立或更新客戶資料；其他內部成員維持查詢權限。
    if user_id == OWNER_USER_ID:
        record = upsert_client_record(group_id, group_name, user_id)
        lines.append(f"客戶名稱：{record.get('clientName', group_name)}")
        lines.append("Google Sheet：已自動登記")
    else:
        record = get_client_record(group_id)
        if record:
            lines.append(f"客戶名稱：{record.get('clientName', '（未設定）')}")
        else:
            lines.append("客戶名稱：尚未綁定")

    return "\n".join(lines)


@app.post("/webhook")
async def webhook(request: Request):
    """接收 LINE Webhook，並處理文字、圖片與圖卡回傳事件。"""
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    payload = await request.json()

    for event in payload.get("events", []):
        source = event.get("source", {})
        user_id = source.get("userId", "")
        reply_token = event.get("replyToken", "")

        # 代墊登記只允許已登記成員在 Bot 個人聊天室操作。
        if event.get("type") == "postback" and source.get("type") == "user":
            if user_id not in INTERNAL_USER_IDS:
                continue
            postback = event.get("postback", {})
            raw_data = postback.get("data", "")
            if not raw_data.startswith("expense:"):
                continue
            session = get_expense_session(user_id)
            if raw_data == "expense:cancel":
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, "本次代墊登記已取消。")
                continue
            if not session:
                reply_text(reply_token, "登記已逾時，請重新輸入「代墊」。")
                continue
            parts = raw_data.split(":", 2)
            action = parts[1]
            value = parts[2] if len(parts) > 2 else ""
            if action == "date":
                value = postback.get("params", {}).get("date", value)
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    reply_text(reply_token, "日期格式不正確，請重新選擇。")
                    continue
                session["data"]["date"] = value
                session["data"]["month"] = str(int(value[5:7]))
                session["step"] = "project"
            elif action in {"category", "payer", "payment", "reimbursed", "invoice"}:
                session["data"][action] = value
                step_order = {"category": "payer", "payer": "payment", "payment": "reimbursed", "reimbursed": "invoice", "invoice": "receipt"}
                session["step"] = step_order[action]
            elif action == "receipt" and value == "略過收據":
                session["step"] = "note"
            elif action == "confirm":
                try:
                    session["data"]["registrantName"] = get_line_profile_name(user_id)
                    submit_expense(session["data"])
                except requests.RequestException:
                    reply_text(reply_token, "目前無法寫入支出資料，內容已保留，請稍後再按一次「確認送出」。")
                    continue
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, "代墊登記完成，資料已寫入公司支出簿。")
                continue
            else:
                continue
            reply_messages(reply_token, [next_prompt(session)])
            continue

        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        message_type = message.get("type")

        if message_type == "image" and source.get("type") == "user":
            session = get_expense_session(user_id)
            if not session or session.get("step") != "receipt":
                continue
            try:
                receipt_base64, receipt_mime = download_line_image(message.get("id", ""))
            except requests.RequestException:
                reply_text(reply_token, "收據照片讀取失敗，請重新傳送一次。")
                continue
            session["data"]["receiptBase64"] = receipt_base64
            session["data"]["receiptMimeType"] = receipt_mime
            session["step"] = "note"
            reply_messages(reply_token, [next_prompt(session)])
            continue

        if message_type != "text":
            continue

        text = message.get("text", "").strip()
        command = text.casefold()

        if command == "代墊":
            if source.get("type") != "user":
                reply_text(reply_token, "代墊登記請在 Bot 個人聊天室使用。")
                continue
            if user_id not in INTERNAL_USER_IDS:
                reply_text(reply_token, "你的帳號尚未加入公司內部登記名單。")
                continue
            new_expense_session(user_id)
            reply_messages(reply_token, [date_card()])
            continue

        session = get_expense_session(user_id) if source.get("type") == "user" else None
        if session:
            step = session["step"]
            if step == "project":
                session["data"]["project"] = text
                session["step"] = "item"
            elif step == "item":
                session["data"]["item"] = text
                session["step"] = "amount"
            elif step == "amount":
                normalized = text.replace(",", "").replace("$", "")
                try:
                    amount = float(normalized)
                    if amount <= 0:
                        raise ValueError
                except ValueError:
                    reply_text(reply_token, "金額必須是大於 0 的數字，請重新輸入。")
                    continue
                session["data"]["amount"] = int(amount) if amount.is_integer() else amount
                session["step"] = "category"
            elif step == "note":
                if not session["data"].get("receiptBase64") and text in {"無", "沒有", ""}:
                    reply_text(reply_token, "未附收據時，備註必須說明原因。")
                    continue
                session["data"]["note"] = text
                session["step"] = "confirm"
            else:
                reply_messages(reply_token, [next_prompt(session)])
                continue
            reply_messages(reply_token, [next_prompt(session)])
            continue

        if command == "my id":
            result = f"User ID：{user_id}" if user_id else "目前無法取得 User ID。"
        elif command == "id":
            # ID 指令會回傳 Group ID；內部資料只讓已登記的三位夥伴查詢。
            if user_id not in INTERNAL_USER_IDS:
                continue
            try:
                result = build_group_reply(source, user_id)
            except requests.RequestException:
                result = "目前無法取得群組資料，請稍後再試。"
        else:
            continue

        try:
            reply_text(reply_token, result)
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
        "expense_configured": bool(EXPENSE_API_URL and EXPENSE_API_KEY),
        "internal_user_count": len(INTERNAL_USER_IDS),
        "owner_configured": bool(OWNER_USER_ID),
    }


@app.get("/")
async def root():
    """提供部署狀態說明，不顯示任何敏感憑證。"""
    return {"service": "AURTOR LINE Bot", "status": "running"}
