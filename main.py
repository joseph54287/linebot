"""AURTOR LINE Bot：共用 Webhook、內部 ID 查詢與客戶群組辨識。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_RELEASE = "2026-08-09-expense-v15"
import external_case
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GROUP_REGISTRY_URL = os.environ.get("GROUP_REGISTRY_URL", "").rstrip("/")
GROUP_REGISTRY_API_KEY = os.environ.get("GROUP_REGISTRY_API_KEY", "")
EXPENSE_API_URL = os.environ.get("EXPENSE_API_URL", GROUP_REGISTRY_URL).rstrip("/")
EXPENSE_API_KEY = os.environ.get("EXPENSE_API_KEY", GROUP_REGISTRY_API_KEY)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RECEIPT_VISION_MODEL = os.environ.get("RECEIPT_VISION_MODEL", "gemini-2.5-flash")
PROJECT_API_URL = os.environ.get("PROJECT_API_URL", "").rstrip("/")
PROJECT_API_KEY = os.environ.get("PROJECT_API_KEY", "")
BONUS_API_URL = os.environ.get("BONUS_API_URL", "").rstrip("/")
BONUS_API_KEY = os.environ.get("BONUS_API_KEY", "")
QUOTE_WEBHOOK_URL = os.environ.get(
    "QUOTE_WEBHOOK_URL",
    "https://linebot-bam2.onrender.com/webhook",
).rstrip("/")
QUOTE_OWNER_USER_ID = "Ub983deb79584603885e5b28e9fdf2d5d"
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
CALENDAR_IDS = tuple(
    calendar_id.strip()
    for calendar_id in os.environ.get(
        "CALENDAR_IDS",
        "contact@goalbrother.com,aurtorfilm@gmail.com",
    ).split(",")
    if calendar_id.strip()
)
OWNER_USER_ID = os.environ.get(
    "OWNER_USER_ID",
    "U6c6441cb38102499d1f80d4ea79a53ab",
)
# 外案最終核准人固定為高爾賢的 LINE 帳號；避免其他系統的 OWNER_USER_ID 誤導核准通知。
EXTERNAL_CASE_OWNER_USER_ID = os.environ.get("EXTERNAL_CASE_OWNER_USER_ID", QUOTE_OWNER_USER_ID)
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
# 完成第一筆後保留專案 15 分鐘，讓員工可連續上傳整疊單據。
EXPENSE_BATCHES: dict[str, dict[str, Any]] = {}
BATCH_TTL_SECONDS = 15 * 60
RECENT_EXPENSE_PROJECTS: dict[str, dict[str, Any]] = {}
RECENT_PROJECT_TTL_SECONDS = 24 * 60 * 60

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
EXPENSE_ITEM_OPTIONS = [
    "交通", "餐飲", "道具", "場景", "器材", "演員", "服裝", "其他工作人員", "後期",
]
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
LOGGER = logging.getLogger("aurtor-line-bot")

# 保存目前 Webhook 事件資訊，讓既有回覆呼叫能在 Reply Token 逾時時安全改用 Push。
CURRENT_LINE_USER_ID: ContextVar[str] = ContextVar("current_line_user_id", default="")
CURRENT_LINE_SOURCE_TYPE: ContextVar[str] = ContextVar("current_line_source_type", default="")
CURRENT_WEBHOOK_EVENT_ID: ContextVar[str] = ContextVar("current_webhook_event_id", default="")
DELIVERED_LINE_EVENTS: dict[str, float] = {}
DELIVERED_EVENT_TTL_SECONDS = 30 * 60

# 專案狀態會由不同系統提供，統一轉成小寫後排除已結束項目。
CLOSED_PROJECT_STATUSES = {
    "closed", "completed", "cancelled", "canceled", "archived",
    "已結案", "結案", "已完成", "完成", "已取消", "取消", "封存", "已封存",
}

# 依 LINE User ID 自動帶入支出人，避免員工每次重複輸入姓名。
INTERNAL_USER_NAMES = {
    "U6c6441cb38102499d1f80d4ea79a53ab": "周暐",
    "Ub983deb79584603885e5b28e9fdf2d5d": "高爾賢",
    "U9478b00702c716685d9d8b021d62d538": "阿全",
}
# 獎金表使用的正式姓名必須與 COUNTIF 關鍵字完全一致。
EXTERNAL_CASE_NAMES = {
    "U6c6441cb38102499d1f80d4ea79a53ab": "周暐",
    "Ub983deb79584603885e5b28e9fdf2d5d": "爾賢",
    "U9478b00702c716685d9d8b021d62d538": "阿筌",
}

CATEGORY_KEYWORDS = {
    "案件支出（餐飲、道具、人員...）": [
        "餐", "便當", "飲料", "道具", "演員", "人員", "車馬", "住宿", "場地", "器材費",
        "吊車", "起重", "吊掛", "機具租賃", "高空車", "堆高機", "加油", "汽油", "柴油",
        "油資", "燃料", "中油", "台塑", "服裝", "造型", "後期", "剪輯", "調光", "混音",
    ],
    "例行性支出(水電費/房租)": ["水費", "電費", "房租", "瓦斯", "網路費"],
    "人事費用(商業保險費,薪資 ,勞保,健保,勞退）": ["薪資", "勞保", "健保", "勞退", "保險"],
    "工具設備（軟體/硬體）": ["硬碟", "軟體", "訂閱", "電池", "線材", "設備", "鏡頭", "電腦"],
    "會計支出(營業稅,營所稅,申報費)": ["營業稅", "營所稅", "申報", "會計"],
    "公司雜費": ["停車", "油錢", "郵資", "寄件", "鑰匙", "清潔", "文具"],
    "業務開發": ["業務", "招待", "提案", "拜訪"],
    "行銷費用": ["廣告投放", "行銷", "社群", "宣傳"],
}

# 員工只需要理解製作現場的大項目；Google Form 的既有分類由系統同步帶入。
ITEM_KEYWORDS = {
    "交通": ["交通", "加油", "汽油", "柴油", "油資", "燃料", "中油", "台塑", "停車", "計程車", "高鐵", "火車", "租車"],
    "餐飲": ["餐飲", "餐費", "便當", "飲料", "咖啡", "早餐", "午餐", "晚餐", "點心"],
    "道具": ["道具", "美術材料", "佈置用品"],
    "場景": ["場景", "場地", "棚租", "攝影棚", "租棚"],
    "器材": ["器材", "鏡頭", "燈具", "相機", "硬碟", "電池", "線材", "吊車", "高空車", "堆高機"],
    "演員": ["演員", "臨演", "模特兒", "model"],
    "服裝": ["服裝", "治裝", "造型", "妝髮"],
    "其他工作人員": ["工作人員", "攝影師", "燈光師", "收音師", "場務", "製片", "助理"],
    "後期": ["後期", "剪輯", "調光", "混音", "動畫", "字幕", "特效"],
}
PROJECT_EXPENSE_CATEGORY = "案件支出（餐飲、道具、人員...）"
COMPANY_TAX_ID = "90531465"
COMPANY_TAX_ID_MISSING = "公司統編不符合"

FIELD_LABELS = {
    "project": ["專案", "案件"],
    "item": ["項目", "品項", "內容"],
    "amount": ["金額", "費用"],
}


def is_quote_event(event: dict[str, Any]) -> bool:
    """只辨識高爾賢個人聊天室內明確定義的報價操作。"""
    source = event.get("source", {})
    if source.get("type") != "user" or source.get("userId") != QUOTE_OWNER_USER_ID:
        return False

    if event.get("type") == "postback":
        data = event.get("postback", {}).get("data", "")
        fields = {key: values[-1] for key, values in parse_qs(data).items()}
        return (
            fields.get("action") == "scheme"
            and bool(fields.get("invitation"))
            and fields.get("scheme") in {"A", "B", "C"}
        )

    if event.get("type") == "message":
        message = event.get("message", {})
        if message.get("type") != "text":
            return False
        text = message.get("text", "").strip()
        return text in {"確認", "送出"} or text.startswith("主旨：")
    return False


def calendar_query_intent(text: str) -> str | None:
    """依訊息中的日期關鍵字判斷要查詢的行程範圍。"""
    normalized = re.sub(r"[\s的]", "", text.strip())
    phrase_groups = (
        ("week", ("這週行程", "本週行程")),
        ("day_after_tomorrow", ("後天行程",)),
        ("tomorrow", ("明天行程", "明日行程")),
        ("today", ("今天行程", "今日行程")),
    )
    for intent, phrases in phrase_groups:
        if any(phrase in normalized for phrase in phrases):
            return intent
    return "today_tomorrow" if normalized == "行程" else None


def is_calendar_command(event: dict[str, Any]) -> bool:
    """只允許三位內部成員在 Bot 個人聊天室查詢指定日期範圍的行程。"""
    source = event.get("source", {})
    message = event.get("message", {})
    return (
        event.get("type") == "message"
        and source.get("type") == "user"
        and source.get("userId") in INTERNAL_USER_IDS
        and message.get("type") == "text"
        and calendar_query_intent(message.get("text", "")) is not None
    )


def forward_quote_webhook(body: bytes, signature: str) -> bool:
    """原樣轉送 LINE request；失敗不重試，避免重複寄出報價信。"""
    try:
        response = requests.post(
            QUOTE_WEBHOOK_URL,
            data=body,
            headers={"Content-Type": "application/json", "X-Line-Signature": signature},
            timeout=20,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        LOGGER.error("報價 Webhook 轉送失敗：%s", type(error).__name__)
        return False


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


def purge_delivered_line_events() -> None:
    """移除過期事件，避免 Render 記憶體持續累積。"""
    cutoff = time.time() - DELIVERED_EVENT_TTL_SECONDS
    for event_id, delivered_at in list(DELIVERED_LINE_EVENTS.items()):
        if delivered_at < cutoff:
            DELIVERED_LINE_EVENTS.pop(event_id, None)


def push_messages(user_id: str, messages: list[dict[str, Any]]) -> None:
    """Reply Token 已失效時，將相同內容 Push 至原本的員工個人聊天室。"""
    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=line_headers(),
        json={"to": user_id, "messages": messages},
        timeout=10,
    )
    response.raise_for_status()


def should_fallback_to_push(error: requests.RequestException) -> bool:
    """只對逾時、連線錯誤、Reply Token 失效或 LINE 暫時異常啟用備援。"""
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    response = getattr(error, "response", None)
    if response is None:
        return False
    return response.status_code in {400, 408, 409, 410, 429, 500, 502, 503, 504}


def reply_messages(reply_token: str, messages: list[dict[str, Any]]) -> None:
    """優先使用 Reply API；逾時時僅對內部員工個人聊天室改用 Push。"""
    event_id = CURRENT_WEBHOOK_EVENT_ID.get()
    user_id = CURRENT_LINE_USER_ID.get()
    source_type = CURRENT_LINE_SOURCE_TYPE.get()
    purge_delivered_line_events()
    if event_id and event_id in DELIVERED_LINE_EVENTS:
        LOGGER.info("略過已送達的 LINE 重送事件：%s", event_id)
        return

    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=line_headers(),
            json={"replyToken": reply_token, "messages": messages},
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        can_push = (
            source_type == "user"
            and user_id in INTERNAL_USER_IDS
            and should_fallback_to_push(error)
        )
        if not can_push:
            raise
        LOGGER.warning("LINE Reply 失敗，改用 Push 備援：%s", type(error).__name__)
        push_messages(user_id, messages)

    if event_id:
        DELIVERED_LINE_EVENTS[event_id] = time.time()


def reply_text(reply_token: str, text: str) -> None:
    """回覆單一文字訊息。"""
    reply_messages(reply_token, [{"type": "text", "text": text}])


def google_access_token() -> str:
    """以 Render Secret 中的 refresh token 取得短效 Google access token。"""
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
        raise requests.RequestException("Google Calendar OAuth 尚未設定")
    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": GOOGLE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    response.raise_for_status()
    token = response.json().get("access_token", "")
    if not token:
        raise requests.RequestException("Google OAuth 未回傳 access token")
    return token


def fetch_calendar_events(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """合併指定兩本 Google Calendar，並去除完全相同的事件。"""
    token = google_access_token()
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for calendar_id in CALENDAR_IDS:
        response = requests.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{requests.utils.quote(calendar_id, safe='')}/events",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeZone": "Asia/Taipei",
            },
            timeout=15,
        )
        response.raise_for_status()
        for item in response.json().get("items", []):
            if item.get("status") == "cancelled":
                continue
            event_start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date", "")
            event_end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date", "")
            key = (
                item.get("summary", "未命名行程"),
                event_start,
                event_end,
                item.get("location", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            events.append(item)

    def sort_key(item: dict[str, Any]) -> str:
        return item.get("start", {}).get("dateTime") or item.get("start", {}).get("date", "")

    return sorted(events, key=sort_key)


def calendar_event_time(item: dict[str, Any], include_date: bool = False) -> str:
    """將 Google Calendar 起訖時間轉為台北時間。"""
    start_data = item.get("start", {})
    if start_data.get("date"):
        return f"{start_data['date'][5:].replace('-', '/')} 全天" if include_date else "全天"
    try:
        start = datetime.fromisoformat(start_data["dateTime"].replace("Z", "+00:00")).astimezone(TAIPEI_TZ)
        end = datetime.fromisoformat(item["end"]["dateTime"].replace("Z", "+00:00")).astimezone(TAIPEI_TZ)
    except (KeyError, ValueError):
        return "時間待確認"
    prefix = f"{start:%m/%d} " if include_date else ""
    return f"{prefix}{start:%H:%M}–{end:%H:%M}"


def calendar_day_card(
    label: str,
    day: datetime,
    events: list[dict[str, Any]],
    *,
    subtitle: str | None = None,
    include_event_date: bool = False,
) -> dict[str, Any]:
    """建立單日或日期範圍的藍色 LINE 行程圖卡。"""
    rows: list[dict[str, Any]] = []
    for item in events[:12]:
        details = calendar_event_time(item, include_event_date)
        if item.get("location"):
            details += f"｜{item['location']}"
        row_contents: list[dict[str, Any]] = [
            {"type": "text", "text": item.get("summary") or "未命名行程", "weight": "bold", "size": "sm", "wrap": True},
            {"type": "text", "text": details, "size": "xs", "color": "#64748B", "wrap": True, "margin": "xs"},
        ]
        link = item.get("hangoutLink") or item.get("htmlLink")
        row: dict[str, Any] = {
            "type": "box", "layout": "vertical", "margin": "md", "paddingAll": "12px",
            "backgroundColor": "#EFF6FF", "cornerRadius": "12px", "contents": row_contents,
        }
        if link:
            row["action"] = {"type": "uri", "label": "開啟行程", "uri": link}
        rows.append(row)
    if not rows:
        rows.append({"type": "text", "text": "沒有行程", "align": "center", "color": "#64748B", "margin": "xl"})
    elif len(events) > 12:
        rows.append({"type": "text", "text": f"另有 {len(events) - 12} 項行程，請至 Google Calendar 查看", "size": "xs", "color": "#64748B", "wrap": True, "margin": "md"})

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#2563EB", "paddingAll": "20px",
            "contents": [
                {"type": "text", "text": label, "color": "#FFFFFF", "weight": "bold", "size": "xl"},
                {"type": "text", "text": subtitle or day.strftime("%Y/%m/%d"), "color": "#DBEAFE", "size": "sm", "margin": "sm"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": rows},
    }


def calendar_command_message(now: datetime | None = None, text: str = "行程") -> dict[str, Any]:
    """依日期關鍵字查詢單日、本週，或預設的今日與明日。"""
    current = (now or datetime.now(TAIPEI_TZ)).astimezone(TAIPEI_TZ)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    intent = calendar_query_intent(text)

    day_queries = {
        "today": ("今日行程", today, tomorrow),
        "tomorrow": ("明日行程", tomorrow, day_after),
        "day_after_tomorrow": ("後天行程", day_after, day_after + timedelta(days=1)),
    }
    if intent in day_queries:
        label, start, end = day_queries[intent]
        return {
            "type": "flex",
            "altText": label,
            "contents": calendar_day_card(label, start, fetch_calendar_events(start, end)),
        }

    if intent == "week":
        week_start = today - timedelta(days=today.weekday())
        next_week = week_start + timedelta(days=7)
        week_end = next_week - timedelta(days=1)
        label = "本週行程"
        return {
            "type": "flex",
            "altText": label,
            "contents": calendar_day_card(
                label,
                week_start,
                fetch_calendar_events(week_start, next_week),
                subtitle=f"{week_start:%m/%d}–{week_end:%m/%d}",
                include_event_date=True,
            ),
        }

    today_events = fetch_calendar_events(today, tomorrow)
    tomorrow_events = fetch_calendar_events(tomorrow, day_after)
    return {
        "type": "flex",
        "altText": "今日與明日行程",
        "contents": {
            "type": "carousel",
            "contents": [
                calendar_day_card("今日行程", today, today_events),
                calendar_day_card("明日行程", tomorrow, tomorrow_events),
            ],
        },
    }


def start_loading(user_id: str, seconds: int = 60) -> None:
    """在一對一聊天室顯示 LINE 原生處理動畫；失敗不阻斷主流程。"""
    if not user_id:
        return
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            headers=line_headers(),
            json={"chatId": user_id, "loadingSeconds": seconds},
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException:
        LOGGER.warning("無法顯示 LINE 載入動畫")


def external_next_message(session: dict[str, Any]) -> dict[str, Any]:
    if session["step"] == "confirm":
        return external_case.confirmation_card(session["data"])
    return external_case.prompt(session["step"])


def ui_header(title: str, eyebrow: str = "") -> dict[str, Any]:
    """建立確認版深藍大標題區。"""
    contents: list[dict[str, Any]] = []
    if eyebrow:
        contents.append({"type": "text", "text": eyebrow, "color": "#B9C9DD", "size": "sm", "weight": "bold"})
    contents.append({"type": "text", "text": title, "color": "#FFFFFF", "size": "xl", "weight": "bold", "margin": "md", "wrap": True})
    return {"type": "box", "layout": "vertical", "backgroundColor": "#193B65", "paddingAll": "24px", "contents": contents}


def ui_row(label: str, value: Any, color: str = "#111827", value_size: str = "md") -> dict[str, Any]:
    """建立左右對齊的欄位列。"""
    return {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
        {"type": "text", "text": label, "color": "#64748B", "size": "sm", "weight": "bold", "flex": 4},
        {"type": "text", "text": str(value), "color": color, "size": value_size, "weight": "bold", "wrap": True, "flex": 6},
    ]}


def ui_button(label: str, data: str, kind: str = "outline", display_text: str | None = None) -> dict[str, Any]:
    """建立主按鈕、描邊按鈕或低優先文字按鈕。"""
    button: dict[str, Any] = {
        "type": "button", "height": "sm", "margin": "md",
        "action": {"type": "postback", "label": label[:20], "data": data, "displayText": display_text or label},
    }
    if kind == "primary":
        button.update({"style": "primary", "color": "#06C755"})
    elif kind == "navy":
        button.update({"style": "primary", "color": "#193B65"})
    elif kind == "cancel":
        button["color"] = "#94A3B8"
    else:
        button["style"] = "secondary"
    return button


def ui_warning(text: str, tone: str = "warning") -> dict[str, Any]:
    """建立橘色或紅色狀態提示框。"""
    palette = {
        "warning": ("#FFF7E6", "#B45309"), "error": ("#FEF2F2", "#DC2626"),
        "success": ("#ECFDF5", "#047857"), "info": ("#F1F5F9", "#64748B"),
    }
    background, color = palette.get(tone, palette["warning"])
    return {"type": "box", "layout": "vertical", "backgroundColor": background, "cornerRadius": "12px", "paddingAll": "16px", "margin": "lg", "contents": [
        {"type": "text", "text": text, "color": color, "size": "sm", "weight": "bold", "wrap": True},
    ]}


def ui_card(title: str, body: list[dict[str, Any]], buttons: list[dict[str, Any]] | None = None, eyebrow: str = "") -> dict[str, Any]:
    """組合確認版 Flex Bubble。"""
    bubble: dict[str, Any] = {
        "type": "bubble", "size": "kilo", "header": ui_header(title, eyebrow),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "24px", "contents": body},
    }
    if buttons:
        bubble["footer"] = {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": buttons}
    return {"type": "flex", "altText": title, "contents": bubble}


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


def project_candidate_card(projects: list[dict[str, Any]], page: int = 0) -> dict[str, Any]:
    """分頁顯示未結案專案；第一頁優先提供七筆，避免選項過長。"""
    page = max(0, page)
    page_size = 7 if page == 0 else 6
    start = 0 if page == 0 else 7 + (page - 1) * 6
    page_projects = projects[start:start + page_size]
    buttons = [ui_button(str(project["name"]), f"expense:project:{start + index}") for index, project in enumerate(page_projects)]
    if page > 0:
        buttons.append(ui_button("上一頁", f"expense:project_page:{page - 1}", display_text="上一頁專案"))
    if start + page_size < len(projects):
        buttons.append(ui_button("下一頁", f"expense:project_page:{page + 1}", display_text="下一頁專案"))
    if not page_projects:
        buttons.append(ui_button("選擇近期專案", "expense:project:search", "primary"))
    buttons.extend([
        ui_button("手動輸入專案名稱", "expense:project:manual"),
        ui_button("取消登記", "expense:cancel", "cancel"),
    ])
    body = [
        ui_warning("✓ 單據已辨識完成，尚未建立紀錄", "success"),
        {"type": "text", "text": "系統已依單據內容判斷費用大方向；若屬於客戶專案，請選擇或輸入專案名稱。", "color": "#475569", "wrap": True, "margin": "lg"},
        ui_row("系統判斷分類", "公司營運" if not page_projects else "案件支出"),
    ]
    return ui_card("選擇代墊專案", body, buttons, "步驟 1 / 2")


def company_tax_invalid_card() -> dict[str, Any]:
    """買方統編不正確時阻擋送出，且不在 LINE 顯示任何辨識到的號碼。"""
    return {
        "type": "template", "altText": "公司統編未通過驗證", "template": {
            "type": "buttons", "title": "無法確認公司代墊", "text": "單據上未辨識到正確的公司統編，請重新拍攝完整單據。",
            "actions": [
                {"type": "postback", "label": "重新拍攝", "data": "expense:retake", "displayText": "重新拍攝收據"},
                {"type": "postback", "label": "取消登記", "data": "expense:cancel", "displayText": "取消登記"},
            ],
        },
    }


def item_option_card() -> dict[str, Any]:
    """顯示九個製作項目與取消按鈕，避免員工自行猜分類名稱。"""
    message = option_card("請選擇消費項目", EXPENSE_ITEM_OPTIONS, "item")
    message["contents"]["body"]["contents"].append({
        "type": "button",
        "height": "sm",
        "margin": "sm",
        "action": {"type": "postback", "label": "取消登記", "data": "expense:cancel", "displayText": "取消登記"},
    })
    return message


def amount_missing_card() -> dict[str, Any]:
    """金額無法辨識時提供明確完成方式與取消選項。"""
    return {
        "type": "template",
        "altText": "請補充支出金額",
        "template": {
            "type": "buttons",
            "title": "還差支出金額",
            "text": "請直接輸入金額，例如：500",
            "actions": [
                {"type": "postback", "label": "重新辨識收據", "data": "expense:retake", "displayText": "重新辨識收據"},
                {"type": "postback", "label": "取消登記", "data": "expense:cancel", "displayText": "取消登記"},
            ],
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


def parse_expense_date(text: str) -> str:
    """從口語日期解析支出日；未提及時預設今天。"""
    today = datetime.now(TAIPEI_TZ).date()
    if "前天" in text:
        return (today - timedelta(days=2)).isoformat()
    if "昨天" in text or "昨日" in text:
        return (today - timedelta(days=1)).isoformat()
    if "明天" in text:
        return (today + timedelta(days=1)).isoformat()

    full_date = re.search(r"(20\d{2})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?", text)
    if full_date:
        try:
            return datetime(*map(int, full_date.groups())).date().isoformat()
        except ValueError:
            pass
    short_date = re.search(r"(?<!\d)(\d{1,2})[月/\-.](\d{1,2})日?", text)
    if short_date:
        try:
            return datetime(today.year, *map(int, short_date.groups())).date().isoformat()
        except ValueError:
            pass
    return today.isoformat()


def extract_labeled_value(text: str, labels: list[str]) -> str:
    """取得「欄位：內容」形式的明確輸入。"""
    label_pattern = "|".join(map(re.escape, labels))
    match = re.search(
        rf"(?:{label_pattern})\s*[：:]?\s*([^，,、；;\n]+)",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def infer_category(text: str) -> str:
    """依消費語意對應既有 Google Form 分類。"""
    scores = {
        category: sum(1 for keyword in keywords if keyword.casefold() in text.casefold())
        for category, keywords in CATEGORY_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""


def infer_item_option(text: str) -> str:
    """把收據或口語內容對應到員工可理解的九個製作項目。"""
    folded = text.casefold()
    scores = {
        item: sum(1 for keyword in keywords if keyword.casefold() in folded)
        for item, keywords in ITEM_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else ""


def infer_expense_content(text: str, item: str = "") -> str:
    """保留員工實際花費內容，與公司大項分類分開。"""
    labeled = extract_labeled_value(text, FIELD_LABELS["item"])
    if labeled:
        return labeled
    folded = text.casefold()
    if any(word in folded for word in ["加油", "汽油", "油資", "燃料", "中油", "台塑"]):
        return "加油／汽油費"
    if "柴油" in folded:
        return "柴油費"
    return item


def infer_payer(text: str, user_id: str) -> str:
    """優先採用文字指定的付款人，否則使用訊息傳送者。"""
    aliases = {
        "周暐": ["周暐", "周偉"],
        "高爾賢": ["高爾賢", "Alex", "導演"],
        "阿全": ["阿全", "筌"],
        "公司": ["公司付", "公司支付"],
        "公司(未付)": ["公司未付"],
    }
    for payer, names in aliases.items():
        if any(name.casefold() in text.casefold() for name in names):
            return payer
    return INTERNAL_USER_NAMES.get(user_id, "")


def infer_amount(text: str) -> int | float | None:
    """安全辨識金額；排除日期、年份與專案名稱中的數字。"""
    explicit = re.search(r"(?:金額|費用)\s*(?:改成|改為|是)?\s*[：:]?\s*[$＄]?\s*([\d,]+(?:\.\d+)?)", text)
    if not explicit:
        explicit = re.search(r"[$＄]\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:元|塊)", text)
    candidates = [next((group for group in match.groups() if group), "") for match in re.finditer(r"[$＄]\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:元|塊)", text)]
    raw = next((group for group in explicit.groups() if group), "") if explicit else ""
    if not raw:
        # 沒有金額單位時，只接受唯一且不是年份或日期片段的數字。
        clean = re.sub(r"20\d{2}[年/\-.]\d{1,2}[月/\-.]\d{1,2}日?", "", text)
        clean = re.sub(r"\d{1,2}[月/\-.]\d{1,2}日?", "", clean)
        numbers = re.findall(r"(?<![A-Za-z])\b(?!20\d{2}\b)(\d[\d,]*(?:\.\d+)?)\b", clean)
        if len(numbers) == 1:
            raw = numbers[0]
    if not raw or len(set(candidates)) > 1:
        return None
    value = float(raw.replace(",", ""))
    if value <= 0:
        return None
    return int(value) if value.is_integer() else value


def infer_project_and_item(text: str, amount: int | float | None) -> tuple[str, str]:
    """從明確標籤或常見口語結構辨識專案與消費項目。"""
    project = ""
    item = extract_labeled_value(text, FIELD_LABELS["item"])
    # 同時支援「PJR 專案」與「這個專案是 MG50」等自然說法。
    project_suffix = re.search(r"(?:^|[^A-Za-z0-9_-])([A-Za-z0-9][A-Za-z0-9_-]{1,39})\s*(?:專案|案件)", text, re.IGNORECASE)
    project_sentence = re.search(r"(?:這個|本次|這筆)?\s*(?:專案|案件)\s*(?:是|為|改成|改為|叫|[：:])\s*([A-Za-z0-9][A-Za-z0-9_-]{1,39})", text, re.IGNORECASE)
    if project_suffix:
        project = project_suffix.group(1)
    elif project_sentence:
        project = project_sentence.group(1)
    project = re.sub(r"^(?:是|為|改成|改為|叫)\s*", "", project).strip()
    cleaned = re.sub(r"^(?:我要|我想)?\s*(?:登記)?\s*代墊\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:今天|昨天|昨日|前天|明天)", "", cleaned)
    cleaned = re.sub(r"20\d{2}[年/\-.]\d{1,2}[月/\-.]\d{1,2}日?", "", cleaned)
    cleaned = re.sub(r"\d{1,2}[月/\-.]\d{1,2}日?", "", cleaned)
    if amount is not None:
        cleaned = re.sub(rf"[$＄]?\s*{re.escape(f'{amount:g}')}\s*(?:元|塊)?", "", cleaned)
    cleaned = re.sub(r"(?:我|周暐|周偉|高爾賢|Alex|阿全|筌|公司)?\s*(?:先付|付的|墊了|代墊|支付)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:還沒|尚未)?領(?:到)?款|未領款|已領款|有統編|無統編|沒發票|無發票|有發票|沒收據|無收據", "", cleaned)
    parts = [part.strip(" ，,、；;。") for part in re.split(r"[，,、；;]|\s+", cleaned) if part.strip(" ，,、；;。")]

    expense_words = [keyword for keywords in CATEGORY_KEYWORDS.values() for keyword in keywords]
    if not item:
        item = next((part for part in parts if any(word in part for word in expense_words)), "")
    if not project and item:
        item_index = parts.index(item) if item in parts else -1
        if item_index > 0:
            project = parts[item_index - 1]
    if not item and len(parts) == 1:
        item = parts[0]
    return project, item


def parse_expense_text(text: str, user_id: str) -> tuple[dict[str, Any], list[str]]:
    """將員工的一句話轉為支出欄位，並一次回傳所有缺漏。"""
    amount = infer_amount(text)
    project, raw_item = infer_project_and_item(text, amount)
    item = infer_item_option(" ".join([raw_item, text]))
    expense_content = infer_expense_content(text, raw_item or item)
    expense_date = parse_expense_date(text)
    data: dict[str, Any] = {
        "registrantUserId": user_id,
        "date": expense_date,
        "month": str(int(expense_date[5:7])),
        "project": project,
        "item": item,
        "expenseContent": expense_content,
        "amount": amount,
        "category": infer_category(" ".join([raw_item, text])) or (PROJECT_EXPENSE_CATEGORY if item else ""),
        "payer": infer_payer(text, user_id),
        "payment": "已支出",
        "reimbursed": "是" if re.search(r"已領(?:到)?款", text) else "否",
        "invoice": "是" if re.search(r"有統編|含統編|統編發票", text) else "未開",
        "note": "；".join(filter(None, [
            f"消費內容：{expense_content}" if expense_content else "",
            "未附收據" if re.search(r"沒收據|無收據", text) else "",
        ])) or "無",
    }
    return data, missing_expense_fields(data)


def merge_expense_text(existing: dict[str, Any], text: str, user_id: str) -> dict[str, Any]:
    """只更新本句能明確辨識的欄位，避免補一句話就清空收據或舊資料。"""
    parsed, _ = parse_expense_text(text, user_id)
    if not existing:
        return parsed
    merged = dict(existing)

    for field in ["project", "item", "expenseContent", "amount", "category"]:
        if parsed.get(field) not in {None, ""}:
            merged[field] = parsed[field]

    if re.search(r"今天|昨天|昨日|前天|明天|20\d{2}[年/\-.]|\d{1,2}[月/\-.]\d{1,2}", text):
        merged["date"] = parsed["date"]
        merged["month"] = parsed["month"]
    if any(name.casefold() in text.casefold() for name in ["周暐", "周偉", "高爾賢", "Alex", "阿全", "筌", "公司付", "公司未付"]):
        merged["payer"] = parsed["payer"]
    if re.search(r"已領(?:到)?款|未領款|尚未領款", text):
        merged["reimbursed"] = parsed["reimbursed"]
    if re.search(r"有統編|含統編|統編發票|無統編|未開", text):
        merged["invoice"] = parsed["invoice"]
    if re.search(r"沒收據|無收據", text):
        merged["note"] = "未附收據"

    merged.setdefault("registrantUserId", user_id)
    merged.setdefault("payer", INTERNAL_USER_NAMES.get(user_id, ""))
    merged.setdefault("payment", "已支出")
    merged.setdefault("reimbursed", "否")
    merged.setdefault("invoice", "未開")
    return merged


def missing_expense_fields(data: dict[str, Any]) -> list[str]:
    """以後端固定規則驗證必要欄位，不直接信任文字解析結果。"""
    required = {
        "project": "專案名稱",
        "item": "消費項目",
        "amount": "金額",
        "payer": "支出人",
    }
    missing = [label for field, label in required.items() if data.get(field) in {None, ""}]
    return missing


def build_missing_prompt(missing: list[str]) -> dict[str, Any]:
    """一次列出所有缺漏欄位，避免逐題追問。"""
    return {
        "type": "text",
        "text": "還差以下資訊就能完成：\n- " + "\n- ".join(missing) + "\n\n請用一句話補充即可。",
    }


def looks_like_expense_intent(text: str) -> bool:
    """辨識沒有寫出「代墊」、但語意仍明確是公司支出的句子。"""
    intent_words = ["我先付", "我墊", "幫公司買", "幫公司付", "先幫公司付", "公司支出"]
    amount = infer_amount(text)
    item = infer_item_option(text)
    has_date = bool(re.search(r"今天|昨天|昨日|前天|20\d{2}[年/\-.]|\d{1,2}[月/\-.]\d{1,2}", text))
    score = sum([amount is not None, bool(item), has_date, any(word in text for word in intent_words)])
    return amount is not None and score >= 2


def looks_like_expense_query(text: str) -> bool:
    """辨識員工是在查詢自己的代墊，而不是新增一筆代墊。"""
    folded = text.casefold()
    expense_words = ["代墊", "墊款", "待撥款", "待領款", "費用", "支出"]
    query_words = ["詢問", "查詢", "情況", "狀況", "統計", "紀錄", "記錄", "進度", "狀態", "多少", "幾筆", "總額", "合計", "我的", "花最多"]
    return any(word in folded for word in expense_words) and any(word in folded for word in query_words)


def looks_like_supplement_query(text: str) -> bool:
    """寬鬆辨識員工查詢本人未完成或待補資料。"""
    folded = text.casefold().replace(" ", "")
    phrases = ["待補件", "代補件", "我要補件", "缺什麼資料", "要補資料", "收據要補", "代墊缺什麼", "資料不完整", "還沒完成的代墊", "剛才那筆要補"]
    return any(phrase in folded for phrase in phrases)


def get_expense_stats(user_id: str) -> dict[str, Any]:
    """從共用支出表取得員工最近一個月的個人代墊統計。"""
    if not EXPENSE_API_URL or not EXPENSE_API_KEY:
        raise requests.RequestException("expense api is not configured")
    payer = INTERNAL_USER_NAMES.get(user_id, "")
    response = requests.get(EXPENSE_API_URL, params={"key": EXPENSE_API_KEY, "action": "expense_stats", "payer": payer, "userId": user_id}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise requests.RequestException("expense stats rejected")
    return payload


def expense_stats_card(stats: dict[str, Any]) -> dict[str, Any]:
    """建立最近一個月統計圖卡，並依專案名稱分類。"""
    projects = stats.get("projects") if isinstance(stats.get("projects"), list) else []
    body = [
        ui_row("統計期間", stats.get("period", "最近一個月")),
        ui_row("總筆數", f"{stats.get('count', 0)} 筆"),
        ui_row("總金額", f"${float(stats.get('total', 0)):g}", "#193B65", "xl"),
        ui_row("待撥款", f"{stats.get('pendingCount', 0)} 筆／${float(stats.get('pendingTotal', 0)):g}"),
        ui_row("已領款", f"{stats.get('paidCount', 0)} 筆／${float(stats.get('paidTotal', 0)):g}"),
    ]
    if projects:
        body.append({"type": "text", "text": "依專案分類", "weight": "bold", "color": "#193B65", "margin": "xl"})
        body.extend(ui_row(str(item.get("project") or "未分類"), f"{item.get('count', 0)} 筆／${float(item.get('total', 0)):g}") for item in projects[:6])
    buttons = [
        ui_button("查看全部明細", "expense:stats_all", "primary"),
        ui_button("選擇專案查看", "expense:stats_project"),
        ui_button("我的待補件", "expense:supplements", "cancel"),
    ]
    return ui_card("我的代墊統計", body, buttons)


def get_supplements(user_id: str) -> list[dict[str, Any]]:
    """取得員工自己的待補件清單。"""
    payer = INTERNAL_USER_NAMES.get(user_id, "")
    response = requests.get(EXPENSE_API_URL, params={"key": EXPENSE_API_KEY, "action": "supplements", "payer": payer, "userId": user_id}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise requests.RequestException("supplement list rejected")
    return payload.get("items", []) if isinstance(payload.get("items"), list) else []


def submit_supplement(user_id: str, row: int, **updates: Any) -> dict[str, Any]:
    """只更新本人原資料列，不建立新的代墊。"""
    supplement = {"userId": user_id, "payer": INTERNAL_USER_NAMES.get(user_id, ""), "row": row, **updates}
    response = requests.post(EXPENSE_API_URL, params={"key": EXPENSE_API_KEY}, json={"action": "supplement", "supplement": supplement}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise requests.RequestException("supplement update rejected")
    return payload


def supplement_list_card(items: list[dict[str, Any]]) -> dict[str, Any]:
    """列出最多十筆待補件資料。"""
    if not items:
        return {"type": "text", "text": "目前沒有待補件資料。"}
    body: list[dict[str, Any]] = []
    for item in items[:10]:
        reasons = "、".join(map(str, item.get("reasons") or []))
        body.append(ui_row(f"#{item.get('row')}  {item.get('project') or '未定專案'}", reasons or "待確認"))
    body.append(ui_warning("補件只更新原資料，不新增第二筆。", "warning"))
    buttons = [ui_button("選擇一筆補資料", "supplement:list:0", "primary")]
    for item in items[:10]:
        label = f"{item.get('project') or '未定專案'}｜${item.get('amount', 0)}"[:20]
        buttons.append(ui_button(label, f"supplement:select:{item.get('row')}"))
    return ui_card("我的待補件", body, buttons)


def supplement_detail_card(item: dict[str, Any]) -> dict[str, Any]:
    """依缺漏原因提供可直接完成的操作。"""
    row = int(item.get("row") or 0)
    reasons = list(map(str, item.get("reasons") or []))
    actions: list[dict[str, Any]] = []
    if "缺少統編" in reasons:
        actions.append({"type": "postback", "label": "維持無統編", "data": f"supplement:accept_no_tax:{row}", "displayText": "維持無統編"})
    if any(reason in reasons for reason in ["缺少統編", "缺少收據", "圖片不清楚"]):
        actions.append({"type": "postback", "label": "重新上傳單據", "data": f"supplement:retake:{row}", "displayText": "重新上傳單據"})
    if "專案待確認" in reasons:
        actions.append({"type": "postback", "label": "補專案名稱", "data": f"supplement:project:{row}", "displayText": "補專案名稱"})
    if "金額需要確認" in reasons:
        actions.append({"type": "postback", "label": "確認金額", "data": f"supplement:amount:{row}", "displayText": "確認金額"})
    actions.append({"type": "postback", "label": "取消", "data": "supplement:cancel:0", "displayText": "取消補件"})
    text = f"日期：{item.get('date') or '未辨識'}\n專案：{item.get('project') or '未確認'}\n金額：${item.get('amount', 0)}\n缺漏：{'、'.join(reasons)}"
    buttons = [{"type": "button", "height": "sm", "style": "primary" if index == 0 else "secondary", "action": action} for index, action in enumerate(actions)]
    return {"type": "flex", "altText": "選擇補件方式", "contents": {"type": "bubble", "header": {"type": "box", "layout": "vertical", "backgroundColor": "#17365D", "contents": [{"type": "text", "text": "補件資料", "color": "#FFFFFF", "weight": "bold"}]}, "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": text, "wrap": True, "size": "sm"}, *buttons]}}}


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


def merge_pending_receipt_text(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """OCR 完成時合併處理期間收到的文字，避免文字與照片被拆成兩筆。"""
    current = EXPENSE_SESSIONS.get(user_id) or {}
    pending_text = str(current.get("pending_text") or "").strip()
    return merge_expense_text(data, pending_text, user_id) if pending_text else data


def get_expense_batch(user_id: str) -> dict[str, Any] | None:
    """取得仍有效的連續代墊模式；逾時即清除專案記憶。"""
    batch = EXPENSE_BATCHES.get(user_id)
    if not batch:
        return None
    if time.time() - batch["updated_at"] > BATCH_TTL_SECONDS:
        EXPENSE_BATCHES.pop(user_id, None)
        return None
    return batch


def get_recent_expense_project(user_id: str) -> str:
    """保留最近一次成功專案 24 小時，供員工快速恢復連續代墊。"""
    record = RECENT_EXPENSE_PROJECTS.get(user_id)
    if not record or time.time() - record["updated_at"] > RECENT_PROJECT_TTL_SECONDS:
        RECENT_EXPENSE_PROJECTS.pop(user_id, None)
        return ""
    return str(record.get("project") or "")


def _project_updated_at(project: dict[str, Any]) -> datetime | None:
    """解析專案系統常見的 ISO 日期格式。"""
    raw = str(project.get("updatedAt") or project.get("createdAt") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def filter_recent_open_projects(
    projects: list[dict[str, Any]],
    context: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """保留全部未結案專案，依內容相關性與更新時間排序。"""
    keywords = {token.casefold() for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", context) if len(token) >= 2}
    candidates: list[tuple[int, datetime, dict[str, Any]]] = []
    for raw_project in projects:
        if not isinstance(raw_project, dict):
            continue
        name = str(raw_project.get("name") or raw_project.get("projectName") or "").strip()
        if not name:
            continue
        status = str(raw_project.get("status") or "").strip().casefold()
        if status in CLOSED_PROJECT_STATUSES:
            continue
        updated_at = _project_updated_at(raw_project)
        aliases = raw_project.get("aliases") if isinstance(raw_project.get("aliases"), list) else []
        searchable = " ".join([name, *map(str, aliases)]).casefold()
        relevance = sum(1 for keyword in keywords if keyword in searchable)
        candidates.append((relevance, updated_at or datetime.min, {
            "id": str(raw_project.get("id") or raw_project.get("projectId") or name),
            "name": name,
            "status": str(raw_project.get("status") or ""),
            "updatedAt": updated_at.isoformat() if updated_at else "",
            "aliases": aliases,
        }))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [project for _, _, project in candidates]


def get_recent_open_projects(context: str = "") -> list[dict[str, Any]]:
    """向未來專案系統取得候選；未設定或失敗時由呼叫端改用手動輸入。"""
    if not PROJECT_API_URL:
        return []
    headers = {"Accept": "application/json"}
    if PROJECT_API_KEY:
        headers["Authorization"] = f"Bearer {PROJECT_API_KEY}"
    response = requests.get(PROJECT_API_URL, headers=headers, timeout=5)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        projects = payload
    elif isinstance(payload, dict):
        projects = payload.get("projects") or payload.get("data") or []
    else:
        projects = []
    if not isinstance(projects, list):
        raise requests.RequestException("project api returned invalid data")
    return filter_recent_open_projects(projects, context)


def build_project_or_missing_prompt(session: dict[str, Any], missing: list[str]) -> dict[str, Any]:
    """依缺漏欄位顯示可直接完成或取消的操作圖卡。"""
    project_label = "專案名稱"
    if project_label in missing:
        context = " ".join([
            str(session.get("raw_text") or ""),
            str(session.get("data", {}).get("item") or ""),
            str(session.get("data", {}).get("note") or ""),
        ])
        try:
            projects = get_recent_open_projects(context)
        except (requests.RequestException, ValueError, TypeError):
            projects = []
        recent_project = str(session.get("recent_project") or "").strip()
        if recent_project and all(str(project.get("name") or "") != recent_project for project in projects):
            projects.insert(0, {"id": f"recent:{recent_project}", "name": recent_project, "status": "最近使用", "updatedAt": "", "aliases": []})
        session["project_candidates"] = projects
        LOGGER.info("expense project candidates count=%s", len(projects))
        return project_candidate_card(projects)
    if "消費項目" in missing or "項目分類或更清楚的消費內容" in missing:
        return item_option_card()
    if "金額" in missing:
        return amount_missing_card()
    return build_missing_prompt(missing)


def next_prompt(session: dict[str, Any]) -> dict[str, Any]:
    """依目前步驟產生下一個問題。"""
    step = session["step"]
    if step == "project":
        return project_candidate_card([])
    if step == "item":
        return item_option_card()
    if step == "amount":
        return {"type": "text", "text": "請輸入支出金額，只輸入數字即可。"}
    if step == "category":
        return item_option_card()
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
    has_receipt = bool(data.get("receiptBase64"))
    body_contents = [
        ui_row("日期", str(data.get("date", "")).replace("-", "/")),
        ui_row("專案", data.get("project", "")),
        ui_row("費用分類", data.get("item") or data.get("category", "")),
        ui_row("消費內容", data.get("expenseContent") or data.get("item", "")),
        ui_row("金額", f"${data.get('amount', '')}", "#193B65", "xl"),
        ui_row("支出人", data.get("payer", "")),
        ui_row("收據", "已儲存" if has_receipt else "待補收據", "#047857" if has_receipt else "#B45309"),
        ui_row("公司統編", "已確認" if data.get("companyTaxIdValid") else "未辨識", "#047857" if data.get("companyTaxIdValid") else "#EA580C"),
        ui_row("特別備註", data.get("specialNote") or "無"),
    ]
    if not data.get("companyTaxIdValid"):
        body_contents.append(ui_warning("此單據未辨識到公司統編，仍可送出，之後會列入待補件。"))
    elif not has_receipt:
        body_contents.append(ui_warning("尚未附上收據；確認送出後會列入待補件。"))
    buttons = [
        ui_button("確認送出", "expense:confirm", "primary"),
        ui_button("送出並連續登記", "expense:confirm_continuous"),
        ui_button("修改資料", "expense:modify"),
        ui_button("取消登記", "expense:cancel", "cancel"),
    ]
    return ui_card("確認代墊資料", body_contents, buttons, "步驟 2 / 2")


def expense_modify_card() -> dict[str, Any]:
    """集中顯示可修改欄位，避免確認卡堆滿按鈕。"""
    labels = [
        ("專案名稱", "project"), ("費用分類", "category"), ("消費內容", "item"),
        ("金額", "amount"), ("日期", "date"), ("重新拍攝單據", "retake"),
    ]
    buttons = [ui_button(label, f"expense:edit:{field}") for label, field in labels]
    buttons.append(ui_button("返回確認資料", "expense:edit:back", "cancel"))
    body = [{"type": "text", "text": "只修改選中的欄位，其他已辨識資料會保留。", "color": "#475569", "wrap": True}]
    return ui_card("選擇要修改的內容", body, buttons, "資料修改")


def expense_result_card(data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """寫入成功後提供可追蹤結果；重複單據則明確阻擋新增。"""
    duplicate = bool(result.get("duplicate"))
    record_url = str(result.get("recordUrl") or "").strip()
    has_receipt = bool(data.get("receiptBase64"))
    success = bool(result.get("ok")) and bool(result.get("row")) and bool(result.get("transactionId")) and (not has_receipt or bool(result.get("receiptUrl")))
    title = "這張單據已登記過" if duplicate else ("代墊登記完成" if success and result.get("receiptUrl") else ("代墊已登記，待補收據" if success else "登記失敗"))
    original = result.get("original") if isinstance(result.get("original"), dict) else {}
    continuous = bool(result.get("continuous"))
    if duplicate:
        body = [
            ui_warning("這張單據可能已由其他員工登記，本次尚未新增資料或儲存第二張圖片。"),
            ui_row("原登記人", original.get("registrantName", "")), ui_row("登記日期", original.get("date", "")),
            ui_row("專案", original.get("project", "")), ui_row("金額", f"${original.get('amount', '')}", "#193B65", "xl"),
            ui_warning(f"比對依據：{result.get('duplicateReason') or '發票號碼、日期與金額相同'}", "info"),
        ]
    elif success:
        body = [
            ui_warning("✓ 資料與收據皆已完成儲存" if result.get("receiptUrl") else "資料已寫入，但仍需補上收據。", "success" if result.get("receiptUrl") else "warning"),
            ui_row("專案", data.get("project", "")), ui_row("金額", f"${data.get('amount', '')}", "#193B65", "xl"),
            ui_row("交易編號", result.get("transactionId", "")),
        ]
    else:
        body = [ui_warning("資料未寫入，沒有建立不完整紀錄。", "error")]
    actions: list[dict[str, Any]] = []
    if record_url.startswith("https://"):
        actions.append({"type": "button", "style": "primary", "color": "#193B65", "height": "sm", "margin": "md", "action": {"type": "uri", "label": "查看原紀錄" if duplicate else "查看登記資料", "uri": record_url}})
    if not duplicate and continuous:
        actions.append(ui_button("登記下一筆", "expense:new", "primary", "登記下一筆代墊"))
        actions.append(ui_button("結束連續登記", "expense:end_batch", "cancel"))
    elif duplicate:
        duplicate_row = int(result.get("row") or 0)
        actions.append(ui_button("確認不是同一筆", f"expense:duplicate_override:{duplicate_row}"))
        actions.append(ui_button("取消本次登記", "expense:cancel", "cancel"))
    elif not success:
        actions.extend([ui_button("重新送出", "expense:confirm", "navy"), ui_button("取消登記", "expense:cancel", "cancel")])
    elif not continuous:
        actions.append(ui_button("完成", "expense:finish_summary", "cancel"))
    return ui_card(title, body, actions, "重複檢查" if duplicate else "登記結果")


def expense_batch_summary_card(batch: dict[str, Any]) -> dict[str, Any]:
    """結束連續模式時回報摘要，只提供開新專案或完成結束。"""
    notes = batch.get("notes", [])
    note_text = "；".join(notes) if notes else "無"
    body = [
        ui_row("專案名稱", batch.get("project", "")), ui_row("登記筆數", f"{batch.get('count', 0)} 筆"),
        ui_row("合計金額", f"${float(batch.get('total', 0)):g}", "#193B65", "xl"),
        ui_row("待撥款", f"{batch.get('pendingCount', batch.get('count', 0))} 筆／${float(batch.get('pendingTotal', batch.get('total', 0))):g}"),
        ui_row("已領款", f"{batch.get('paidCount', 0)} 筆／${float(batch.get('paidTotal', 0)):g}"),
        ui_row("特別備註", note_text),
    ]
    actions = [ui_button("新的專案登記代墊", "expense:start_new", "primary"), ui_button("完成結束", "expense:finish_summary", "cancel")]
    return ui_card("本次代墊摘要", body, actions)


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


def parse_json_object(text: str) -> dict[str, Any]:
    """從 Gemini 回覆中安全取出單一 JSON 物件。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("vision response is not JSON")
    import json

    payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("vision response must be an object")
    return payload


def analyze_receipt_image(image_base64: str, mime_type: str, focused_retry: bool = False) -> dict[str, Any]:
    """使用 Gemini 讀取台灣發票或收據；必要時執行聚焦總額的第二輪。"""
    if not GEMINI_API_KEY:
        raise requests.RequestException("receipt vision is not configured")
    prompt = """你是台灣公司支出單據 OCR 欄位擷取器。只輸出 JSON，不要 Markdown。
不要因為照片角度、皺摺、手寫、裁切或版型陌生就拒絕，請先盡力擷取可見欄位。輸出：
documentType, merchantName, date(YYYY-MM-DD或空字串), items(字串陣列),
totalAmount(數字或null), invoiceNumber, buyerTaxId, sellerTaxId,
rawText(單據上可見文字逐行合併), currency(預設TWD), confidence(0到1), warnings(字串陣列), isReceipt(布林值), imageType(簡短字串)。
buyerTaxId 只能填寫買受人／買方／客戶的統一編號；sellerTaxId 只能填寫商家／賣方的統一編號，兩者不可混用。
即使無法判斷統編屬於買方或賣方，也必須把可見號碼原樣保留在 rawText，不可省略。
totalAmount 必須是整張單據的應付或實付總額，不可使用統編、發票號碼、日期或交易序號。
看不清楚就留空並在 warnings 說明，不要猜測。"""
    if focused_retry:
        prompt += """
這是第二輪校對。圖片中的單據可能只佔一小部分，請先定位紙張、發票或收據區域，忽略桌面、電腦及其他背景，
再像 OCR 人員逐行查看「總計、合計、應付、實付、現金、信用卡、TOTAL」附近數字，
優先找出唯一的最終付款總額與消費日期；若有多個候選，選擇具有總額標籤且位置最接近付款區的數字。"""
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{RECEIPT_VISION_MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": mime_type, "data": image_base64}},
            ]}],
            "generationConfig": {"temperature": 0},
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    try:
        output = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as error:
        raise requests.RequestException("receipt vision returned no result") from error
    try:
        return parse_json_object(output)
    except (ValueError, TypeError) as error:
        raise requests.RequestException("receipt vision returned invalid JSON") from error


def valid_receipt_amount(analysis: dict[str, Any]) -> float | None:
    """只接受合理且大於零的單據總額。"""
    raw_amount = analysis.get("totalAmount")
    try:
        amount = float(str(raw_amount).replace(",", ""))
        return amount if 0 < amount <= 100_000_000 else None
    except (TypeError, ValueError):
        return None


def receipt_signal_count(analysis: dict[str, Any]) -> int:
    """計算日期、商家與品項三種輔助訊號。"""
    items = analysis.get("items") if isinstance(analysis.get("items"), list) else []
    return sum([
        bool(str(analysis.get("date") or "").strip()),
        bool(str(analysis.get("merchantName") or "").strip()),
        any(str(item).strip() for item in items),
    ])


def merge_receipt_analyses(primary: dict[str, Any], retry: dict[str, Any]) -> dict[str, Any]:
    """以第一輪為主，使用第二輪補足空白欄位及更可信的總額。"""
    merged = dict(primary)
    for field in ["documentType", "merchantName", "date", "items", "totalAmount", "invoiceNumber", "buyerTaxId", "sellerTaxId", "rawText", "currency", "imageType"]:
        current_value = merged.get(field)
        if current_value is None or current_value == "" or (field == "items" and not current_value):
            merged[field] = retry.get(field)
    if valid_receipt_amount(primary) is None and valid_receipt_amount(retry) is not None:
        merged["totalAmount"] = retry["totalAmount"]
    merged["isReceipt"] = bool(primary.get("isReceipt") or retry.get("isReceipt"))
    merged["confidence"] = max(float(primary.get("confidence") or 0), float(retry.get("confidence") or 0))
    warnings = []
    primary_warnings = primary.get("warnings") if isinstance(primary.get("warnings"), list) else []
    retry_warnings = retry.get("warnings") if isinstance(retry.get("warnings"), list) else []
    for warning in [*primary_warnings, *retry_warnings]:
        if str(warning) not in warnings:
            warnings.append(str(warning))
    merged["warnings"] = warnings
    merged["usedSecondPass"] = True
    return merged


def normalize_tax_id(value: Any) -> str:
    """統編只保留數字，以便處理空格或連字號，但不接受部分相符。"""
    return re.sub(r"\D", "", str(value or ""))


def has_valid_company_tax_id(analysis: dict[str, Any]) -> bool:
    """買方欄位或 OCR 原文完整出現公司統編即通過，不接受部分相符。"""
    if normalize_tax_id(analysis.get("buyerTaxId")) == COMPANY_TAX_ID:
        return True
    raw_text = str(analysis.get("rawText") or "")
    candidates = re.findall(r"(?<!\d)(\d(?:[\s\-－]?\d){7})(?!\d)", raw_text)
    return any(normalize_tax_id(candidate) == COMPANY_TAX_ID for candidate in candidates)


def receipt_analysis_to_expense(
    analysis: dict[str, Any],
    user_id: str,
    image_base64: str,
    mime_type: str,
) -> tuple[dict[str, Any], list[str]]:
    """驗證影像辨識結果並轉成既有 Google Sheet 支出欄位。"""
    amount = valid_receipt_amount(analysis)
    signals = receipt_signal_count(analysis)
    image_type = str(analysis.get("imageType") or analysis.get("documentType") or "").casefold()
    obvious_non_documents = ["人物", "人像", "風景", "聊天", "對話", "自拍", "person", "landscape", "chat", "screenshot"]
    if amount is None and signals == 0 and (
        analysis.get("isReceipt") is False or any(label in image_type for label in obvious_non_documents)
    ):
        raise ValueError("圖片不是可辨識的發票或收據，請重新拍攝完整單據。")

    raw_date = str(analysis.get("date") or "").strip()
    try:
        expense_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        expense_date = ""

    items = analysis.get("items") if isinstance(analysis.get("items"), list) else []
    items = [str(item).strip() for item in items if str(item).strip()]
    merchant = str(analysis.get("merchantName") or "").strip()
    item_detail = "、".join(items[:8]) or merchant
    item = infer_item_option(" ".join([merchant, item_detail]))
    category = infer_category(" ".join([merchant, item_detail])) or (PROJECT_EXPENSE_CATEGORY if item else "")
    invoice_number = str(analysis.get("invoiceNumber") or "").strip()
    company_tax_id_valid = has_valid_company_tax_id(analysis)
    warnings = analysis.get("warnings") if isinstance(analysis.get("warnings"), list) else []
    confidence = float(analysis.get("confidence") or 0)
    note_parts = [part for part in [
        f"商家：{merchant}" if merchant else "",
        f"單據內容：{item_detail}" if item_detail and item_detail != merchant else "",
        f"發票號碼：{invoice_number}" if invoice_number else "",
    ] if part]
    if confidence < 0.75:
        note_parts.append("影像辨識信心較低，已要求人工確認")
    if warnings:
        note_parts.append("辨識提醒：" + "；".join(map(str, warnings[:3])))

    semantic_signature = "|".join([
        re.sub(r"\W", "", invoice_number).upper(), expense_date,
        str(int(amount) if amount is not None and amount.is_integer() else amount or ""),
        re.sub(r"\s+", "", merchant).casefold(), re.sub(r"\s+", "", item_detail).casefold(),
    ])
    data: dict[str, Any] = {
        "registrantUserId": user_id,
        "date": expense_date,
        "month": str(int(expense_date[5:7])) if expense_date else "",
        "project": "",
        "item": item,
        "amount": int(amount) if amount is not None and amount.is_integer() else amount,
        "category": category,
        "payer": INTERNAL_USER_NAMES.get(user_id, ""),
        "payment": "已支出",
        "reimbursed": "否",
        "invoice": "是" if company_tax_id_valid else "否",
        "companyTaxIdValid": company_tax_id_valid,
        "companyTaxId": COMPANY_TAX_ID if company_tax_id_valid else "",
        "invoiceNumber": invoice_number,
        "merchantName": merchant,
        "note": "；".join(note_parts) or "影像收據自動辨識",
        "receiptBase64": image_base64,
        "receiptMimeType": mime_type,
        "receiptDocumentType": str(analysis.get("documentType") or "單據"),
        "receiptConfidence": confidence,
        "receiptSecondPass": bool(analysis.get("usedSecondPass")),
        "receiptHash": hashlib.sha256(image_base64.encode("ascii")).hexdigest(),
        "receiptSignature": hashlib.sha256(semantic_signature.encode("utf-8")).hexdigest(),
    }
    if not company_tax_id_valid:
        data["note"] = (data["note"] + "；未填寫公司統編").strip("；")
    if amount is None:
        data["note"] = (data["note"] + "；無法辨識總額，請補充金額或重新拍攝").strip("；")
    LOGGER.info(
        "receipt vision type=%s has_amount=%s has_date=%s item_count=%s confidence=%.2f second_pass=%s",
        str(analysis.get("documentType") or analysis.get("imageType") or "unknown")[:30],
        amount is not None,
        bool(expense_date),
        len(items),
        confidence,
        bool(analysis.get("usedSecondPass")),
    )
    missing = missing_expense_fields(data)
    return data, missing


def process_receipt_image(user_id: str, image_base64: str, mime_type: str) -> tuple[dict[str, Any], list[str]]:
    """完成收據辨識與欄位轉換，供 Webhook 與測試共用。"""
    analysis = analyze_receipt_image(image_base64, mime_type)
    receipt_context = " ".join([
        str(analysis.get("merchantName") or ""),
        " ".join(map(str, analysis.get("items") or [])) if isinstance(analysis.get("items"), list) else "",
    ])
    if valid_receipt_amount(analysis) is None or receipt_signal_count(analysis) < 2 or not infer_item_option(receipt_context):
        retry = analyze_receipt_image(image_base64, mime_type, focused_retry=True)
        analysis = merge_receipt_analyses(analysis, retry)
    return receipt_analysis_to_expense(analysis, user_id, image_base64, mime_type)


def submit_expense(data: dict[str, Any]) -> dict[str, Any]:
    """交由 Google Apps Script 上傳收據並新增支出資料。"""
    if not EXPENSE_API_URL or not EXPENSE_API_KEY:
        raise requests.RequestException("expense api is not configured")
    # 同一暫存重試時沿用交易識別碼，讓 Apps Script 避免建立重複附件或資料列。
    data.setdefault("transactionId", uuid.uuid4().hex)
    if data.get("receiptBase64"):
        extension = "png" if data.get("receiptMimeType") == "image/png" else "jpg"
        safe_project = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", str(data.get("project") or "未指定"))[:40]
        data.setdefault(
            "receiptFileName",
            f"{data.get('date') or datetime.now(TAIPEI_TZ).date().isoformat()}_{data.get('payer') or '員工'}_{safe_project}_{data.get('amount') or '待補'}.{extension}",
        )
    response = requests.post(EXPENSE_API_URL, params={"key": EXPENSE_API_KEY}, json={"action": "expense", "expense": data}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        error_code = re.sub(r"[^0-9A-Za-z_-]", "", str(payload.get("error") or "expense_api_rejected"))[:80]
        raise requests.RequestException(error_code or "expense_api_rejected")
    if not payload.get("duplicate"):
        if not payload.get("row") or not payload.get("transactionId"):
            raise requests.RequestException("expense api returned incomplete write confirmation")
        if data.get("receiptBase64") and not str(payload.get("receiptUrl") or "").startswith("https://"):
            raise requests.RequestException("expense receipt was not stored")
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

    # LINE 簽章綁定原始 bytes，因此整批原樣轉送；報價後端會忽略非報價事件。
    if any(is_quote_event(event) for event in payload.get("events", [])):
        forward_quote_webhook(body, signature)

    for event in payload.get("events", []):
        # 報價事件已交由專用後端處理，避免兩邊重複使用 Reply Token。
        if is_quote_event(event):
            continue
        source = event.get("source", {})
        user_id = source.get("userId", "")
        reply_token = event.get("replyToken", "")
        event_id = str(event.get("webhookEventId") or "")

        # LINE 可能因 Webhook 回應延遲而重送同一事件；已成功回覆的事件不得再次執行。
        purge_delivered_line_events()
        if event_id and event_id in DELIVERED_LINE_EVENTS:
            LOGGER.info("忽略 LINE 重送事件：%s", event_id)
            continue
        CURRENT_LINE_USER_ID.set(user_id)
        CURRENT_LINE_SOURCE_TYPE.set(str(source.get("type") or ""))
        CURRENT_WEBHOOK_EVENT_ID.set(event_id)

        # 外案申請獨立於代墊流程：主管核准成功後才寫入獎金表。
        if event.get("type") == "postback" and source.get("type") == "user":
            raw_external = event.get("postback", {}).get("data", "")
            if raw_external.startswith("external:"):
                parts = raw_external.split(":", 3)
                action = parts[1] if len(parts) > 1 else ""
                if action == "cancel":
                    external_case.SESSIONS.pop(user_id, None)
                    reply_text(reply_token, "好，這筆外案先不送出。")
                    continue
                if action == "set" and len(parts) == 4:
                    session = external_case.accept_option(user_id, parts[2], parts[3])
                    if not session:
                        reply_text(reply_token, "這筆外案已逾時，請重新輸入「外案」。")
                    else:
                        reply_messages(reply_token, [external_next_message(session)])
                    continue
                if action == "submit":
                    session = external_case.get_session(user_id)
                    if not session or session["step"] != "confirm":
                        reply_text(reply_token, "資料還沒完整，請重新輸入「外案」。")
                        continue
                    try:
                        external_case.api_call(BONUS_API_URL, BONUS_API_KEY, {"action": "submit", "data": session["data"]})
                    except requests.RequestException:
                        reply_text(reply_token, "目前無法送出核准，內容還在，請稍後再按一次。")
                        continue
                    notification_sent = True
                    try:
                        push_messages(EXTERNAL_CASE_OWNER_USER_ID, [external_case.approval_card(session["data"])])
                    except requests.RequestException:
                        notification_sent = False
                        LOGGER.error("外案已寫入，但主管核准通知推送失敗 request_id=%s", session["data"].get("requestId"))
                    external_case.SESSIONS.pop(user_id, None)
                    message = "已送出，現在是「待核准」。\n有結果我會通知你。"
                    if not notification_sent:
                        message += "\n主管通知暫時未送達，但資料已安全保存，不需要重複送出。"
                    reply_text(reply_token, message)
                    continue
                if action in {"approve", "reject"} and len(parts) >= 3:
                    if user_id != EXTERNAL_CASE_OWNER_USER_ID:
                        reply_text(reply_token, "這個操作只有核准人可以執行。")
                        continue
                    request_id = parts[2]
                    try:
                        result = external_case.api_call(BONUS_API_URL, BONUS_API_KEY, {
                            "action": action, "requestId": request_id, "approverUserId": user_id,
                        })
                    except requests.RequestException:
                        reply_text(reply_token, "目前無法完成處理，這筆尚未登記，請稍後再試。")
                        continue
                    applicant_id = str(result.get("employeeUserId") or "")
                    project_name = str(result.get("projectName") or "這筆外案")
                    if action == "approve":
                        reply_text(reply_token, "已核准，並完成登記。")
                        if applicant_id:
                            push_messages(applicant_id, [{"type": "text", "text": f"你的外案「{project_name}」已經核准，並完成登記。"}])
                    else:
                        reply_text(reply_token, "已拒絕這筆外案。")
                        if applicant_id:
                            push_messages(applicant_id, [{"type": "text", "text": f"你的外案「{project_name}」沒有通過。"}])
                    continue

        # 代墊登記只允許已登記成員在 Bot 個人聊天室操作。
        if event.get("type") == "postback" and source.get("type") == "user":
            if user_id not in INTERNAL_USER_IDS:
                continue
            postback = event.get("postback", {})
            raw_data = postback.get("data", "")
            if raw_data.startswith("supplement:"):
                parts = raw_data.split(":", 2)
                action = parts[1]
                try:
                    row = int(parts[2])
                except (ValueError, IndexError):
                    row = 0
                if action == "cancel":
                    EXPENSE_SESSIONS.pop(user_id, None)
                    reply_text(reply_token, "已取消補件。")
                    continue
                if action == "select":
                    try:
                        item = next(item for item in get_supplements(user_id) if int(item.get("row") or 0) == row)
                        reply_messages(reply_token, [supplement_detail_card(item)])
                    except (requests.RequestException, StopIteration):
                        reply_text(reply_token, "這筆資料已完成或目前無法讀取，請重新輸入「我的待補件」。")
                    continue
                if action == "accept_no_tax":
                    try:
                        result = submit_supplement(user_id, row, acceptNoTax=True)
                        reply_text(reply_token, "已記錄為維持無統編。" if result.get("complete") else "已更新，仍有其他資料需要補件。")
                    except requests.RequestException:
                        reply_text(reply_token, "補件更新失敗，請稍後再試。")
                    continue
                if action in {"retake", "project", "amount"}:
                    step = {"retake": "supplement_image", "project": "supplement_project", "amount": "supplement_amount"}[action]
                    EXPENSE_SESSIONS[user_id] = {"step": step, "supplement_row": row, "updated_at": time.time(), "data": {"registrantUserId": user_id}}
                    prompt = {"retake": "請重新上傳清楚、完整的單據照片。", "project": "請輸入正確的專案名稱。", "amount": "請輸入正確金額，只輸入數字即可。"}[action]
                    reply_text(reply_token, prompt)
                    continue
            if not raw_data.startswith("expense:"):
                continue
            session = get_expense_session(user_id)
            if raw_data == "expense:cancel":
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, "本次代墊登記已取消。")
                continue
            if raw_data == "expense:new":
                project = (get_expense_batch(user_id) or {}).get("project") or get_recent_expense_project(user_id)
                EXPENSE_SESSIONS[user_id] = {"step": "receipt_waiting_image", "updated_at": time.time(), "data": {"registrantUserId": user_id, "project": project}}
                reply_text(reply_token, f"請上傳下一張收據。{'目前沿用專案：' + project if project else ''}")
                continue
            if raw_data == "expense:start_new":
                recent_project = get_recent_expense_project(user_id)
                EXPENSE_BATCHES.pop(user_id, None)
                EXPENSE_SESSIONS[user_id] = {"step": "receipt_waiting_image", "updated_at": time.time(), "recent_project": recent_project, "data": {"registrantUserId": user_id}}
                reply_text(reply_token, "已開始新的專案代墊。請上傳第一張收據，完成辨識後再確認專案。")
                continue
            if raw_data == "expense:finish_summary":
                EXPENSE_BATCHES.pop(user_id, None)
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, "本次代墊已完成結束。")
                continue
            if raw_data == "expense:end_batch":
                batch = EXPENSE_BATCHES.pop(user_id, None)
                if not batch:
                    reply_text(reply_token, "連續代墊模式已結束。")
                else:
                    reply_messages(reply_token, [expense_batch_summary_card(batch)])
                continue
            if raw_data == "expense:supplements":
                try:
                    reply_messages(reply_token, [supplement_list_card(get_supplements(user_id))])
                except requests.RequestException:
                    reply_text(reply_token, "目前無法讀取待補件資料，請稍後再試。")
                continue
            if raw_data in {"expense:stats_all", "expense:stats_project"}:
                if raw_data == "expense:stats_project":
                    EXPENSE_SESSIONS[user_id] = {"step": "stats_project", "updated_at": time.time(), "data": {"registrantUserId": user_id}}
                    reply_text(reply_token, "請輸入要查詢的專案名稱，例如：PJR。")
                else:
                    try:
                        reply_messages(reply_token, [expense_stats_card(get_expense_stats(user_id))])
                    except requests.RequestException:
                        reply_text(reply_token, "目前無法讀取代墊明細，請稍後再試。")
                continue
            if not session:
                reply_text(reply_token, "登記已逾時，請重新輸入「代墊」。")
                continue
            parts = raw_data.split(":", 2)
            action = parts[1]
            value = parts[2] if len(parts) > 2 else ""
            if action == "project_page":
                try:
                    page = int(value)
                except ValueError:
                    page = 0
                reply_messages(reply_token, [project_candidate_card(session.get("project_candidates", []), page)])
                continue
            if action == "project":
                if value in {"manual", "search"}:
                    session["step"] = "project_search"
                    reply_text(reply_token, "請輸入專案關鍵字或完整專案名稱。")
                    continue
                try:
                    project = session.get("project_candidates", [])[int(value)]
                except (ValueError, IndexError, TypeError):
                    reply_text(reply_token, "專案選項已失效，請直接輸入專案名稱。")
                    session["step"] = "project_manual"
                    continue
                session["data"]["project"] = str(project["name"])
                session.pop("project_candidates", None)
                missing = missing_expense_fields(session["data"])
                session["step"] = "quick_missing" if missing else "quick_confirm"
                reply_messages(reply_token, [build_project_or_missing_prompt(session, missing) if missing else build_expense_confirmation(session["data"])])
                continue
            if action == "item" and value in EXPENSE_ITEM_OPTIONS:
                session["data"]["item"] = value
                session["data"]["category"] = PROJECT_EXPENSE_CATEGORY
                missing = missing_expense_fields(session["data"])
                session["step"] = "quick_missing" if missing else "quick_confirm"
                reply_messages(reply_token, [build_project_or_missing_prompt(session, missing) if missing else build_expense_confirmation(session["data"])])
                continue
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
            elif action in {"confirm", "confirm_continuous"}:
                if session.get("submitting"):
                    reply_text(reply_token, "這筆資料正在送出，請勿重複點擊。")
                    continue
                session["submitting"] = True
                session["continuous"] = action == "confirm_continuous"
                try:
                    session["data"]["registrantName"] = get_line_profile_name(user_id)
                    result = submit_expense(session["data"])
                except requests.RequestException as error:
                    session["submitting"] = False
                    error_code = str(error) or "unknown"
                    LOGGER.exception("代墊寫入失敗 code=%s", error_code)
                    reply_text(reply_token, f"目前無法寫入支出資料（錯誤：{error_code[:40]}），內容已保留，請稍後再按一次「確認送出」。")
                    continue
                if result.get("duplicate"):
                    session["submitting"] = False
                    session["duplicateRow"] = result.get("row")
                    reply_messages(reply_token, [expense_result_card(session["data"], result)])
                    continue
                EXPENSE_SESSIONS.pop(user_id, None)
                result["continuous"] = bool(session.get("continuous"))
                if session.get("continuous"):
                    batch = EXPENSE_BATCHES.setdefault(user_id, {"project": session["data"].get("project", ""), "count": 0, "total": 0.0, "notes": [], "hashes": [], "recordUrls": []})
                    batch["project"] = session["data"].get("project", batch.get("project", ""))
                    batch["count"] += 1
                    batch["total"] += float(session["data"].get("amount") or 0)
                    batch["updated_at"] = time.time()
                    RECENT_EXPENSE_PROJECTS[user_id] = {"project": batch["project"], "updated_at": time.time()}
                    if result.get("recordUrl"):
                        batch.setdefault("recordUrls", []).append(result["recordUrl"])
                    receipt_hash = session["data"].get("receiptHash")
                    if receipt_hash and receipt_hash not in batch["hashes"]:
                        batch["hashes"].append(receipt_hash)
                    if session["data"].get("companyTaxIdValid") is not True:
                        batch["notes"].append(f"第 {batch['count']} 筆未填寫公司統編")
                reply_messages(reply_token, [expense_result_card(session["data"], result)])
                continue
            elif action == "change_project":
                session["data"]["project"] = ""
                missing = missing_expense_fields(session["data"])
                session["step"] = "quick_missing"
                reply_messages(reply_token, [build_project_or_missing_prompt(session, missing)])
                continue
            elif action == "modify":
                session["step"] = "quick_edit"
                reply_messages(reply_token, [expense_modify_card()])
                continue
            elif action == "edit":
                edit_prompts = {
                    "project": ("project_manual", "請輸入正確的專案名稱。"),
                    "category": ("quick_edit", "請輸入正確的費用分類。"),
                    "item": ("quick_edit", "請輸入正確的消費內容。"),
                    "amount": ("quick_edit", "請輸入正確金額，例如：950。"),
                    "date": ("quick_edit", "請輸入正確日期，例如：2026/08/09。"),
                }
                if value == "back":
                    session["step"] = "quick_confirm"
                    reply_messages(reply_token, [build_expense_confirmation(session["data"])])
                    continue
                if value == "retake":
                    EXPENSE_SESSIONS[user_id] = {"step": "receipt_waiting_image", "updated_at": time.time(), "data": session["data"]}
                    reply_text(reply_token, "請重新上傳一張完整、清楚且避免反光的收據或發票照片。")
                    continue
                session["step"], prompt = edit_prompts.get(value, ("quick_edit", "請直接輸入要修改的內容。"))
                session["edit_field"] = value
                reply_text(reply_token, prompt)
                continue
            elif action == "duplicate_override":
                session["data"]["duplicateOverride"] = True
                session["data"]["duplicateOriginalRow"] = int(value or 0)
                session["data"]["duplicateOverrideBy"] = user_id
                session["data"]["transactionId"] = uuid.uuid4().hex
                session["submitting"] = True
                try:
                    result = submit_expense(session["data"])
                except requests.RequestException:
                    session["submitting"] = False
                    reply_text(reply_token, "目前無法完成重複放行，資料已保留，請稍後再試。")
                    continue
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_messages(reply_token, [expense_result_card(session["data"], result)])
                continue
            elif action == "retake":
                EXPENSE_SESSIONS[user_id] = {
                    "step": "receipt_waiting_image",
                    "updated_at": time.time(),
                    "data": {"registrantUserId": user_id},
                }
                reply_text(reply_token, "請重新上傳一張完整、清楚且避免反光的收據或發票照片。")
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
            if user_id not in INTERNAL_USER_IDS:
                continue
            session = get_expense_session(user_id)
            try:
                receipt_base64, receipt_mime = download_line_image(message.get("id", ""))
            except requests.RequestException:
                reply_text(reply_token, "收據照片讀取失敗，請重新傳送一次。")
                continue

            if session and session.get("step") == "supplement_image":
                start_loading(user_id)
                try:
                    data, _ = process_receipt_image(user_id, receipt_base64, receipt_mime)
                    result = submit_supplement(
                        user_id, int(session.get("supplement_row") or 0),
                        receiptBase64=receipt_base64, receiptMimeType=receipt_mime,
                        receiptHash=data.get("receiptHash"), companyTaxIdValid=data.get("companyTaxIdValid"),
                        receiptFileName=f"補件_{int(session.get('supplement_row') or 0)}.jpg",
                    )
                except (ValueError, requests.RequestException):
                    reply_text(reply_token, "單據辨識或補件更新失敗，請重新拍攝後再試。")
                    continue
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, "補件完成。" if result.get("complete") else "照片已更新，仍有其他資料需要補件。")
                continue

            # 第一筆完成後的 15 分鐘內，直接把新單據帶入相同專案。
            batch = get_expense_batch(user_id)
            incoming_hash = hashlib.sha256(receipt_base64.encode("ascii")).hexdigest()
            if not session and batch:
                if incoming_hash in batch.get("hashes", []):
                    reply_text(reply_token, "這張單據剛剛已處理過，沒有重複建立。")
                    continue
                try:
                    start_loading(user_id)
                    EXPENSE_SESSIONS[user_id] = {"step": "receipt_processing", "updated_at": time.time(), "pending_text": "", "data": {"registrantUserId": user_id, "project": batch.get("project", "")}}
                    data, missing = process_receipt_image(user_id, receipt_base64, receipt_mime)
                except ValueError:
                    EXPENSE_SESSIONS.pop(user_id, None)
                    reply_text(reply_token, "這張圖片不像發票或收據，因此沒有啟動代墊登記。")
                    continue
                except requests.RequestException:
                    EXPENSE_SESSIONS.pop(user_id, None)
                    reply_text(reply_token, "目前無法辨識單據，請稍後重新上傳。")
                    continue
                data["project"] = batch.get("project", "")
                data = merge_pending_receipt_text(user_id, data)
                missing = missing_expense_fields(data)
                new_session = {"step": "quick_missing" if missing else "quick_confirm", "updated_at": time.time(), "raw_text": "", "data": data}
                EXPENSE_SESSIONS[user_id] = new_session
                reply_messages(reply_token, [build_project_or_missing_prompt(new_session, missing) if missing else build_expense_confirmation(data)])
                continue

            # 已先輸入「代墊」時，收到圖片就立即進行辨識。
            if session and session.get("step") == "receipt_waiting_image":
                try:
                    start_loading(user_id)
                    session["step"] = "receipt_processing"
                    session["pending_text"] = ""
                    data, missing = process_receipt_image(user_id, receipt_base64, receipt_mime)
                except ValueError as error:
                    session["step"] = "receipt_waiting_image"
                    reply_text(reply_token, str(error))
                    continue
                except requests.RequestException:
                    session["step"] = "receipt_waiting_image"
                    session["receiptBase64"] = receipt_base64
                    session["receiptMimeType"] = receipt_mime
                    reply_text(reply_token, "目前無法辨識收據，圖片已保留，請稍後再輸入「代墊」重試。")
                    continue
                data = merge_pending_receipt_text(user_id, data)
                remembered_project = session.get("data", {}).get("project") or (get_expense_batch(user_id) or {}).get("project")
                if remembered_project:
                    data["project"] = remembered_project
                    missing = missing_expense_fields(data)
                EXPENSE_SESSIONS[user_id] = {
                    "step": "quick_missing" if missing else "quick_confirm",
                    "updated_at": time.time(),
                    "raw_text": "",
                    "data": data,
                    "recent_project": session.get("recent_project", ""),
                }
                reply_messages(reply_token, [build_project_or_missing_prompt(EXPENSE_SESSIONS[user_id], missing) if missing else build_expense_confirmation(data)])
                continue

            # 員工直接上傳圖片時立即啟動辨識，不要求再輸入一次「代墊」。
            if not session:
                try:
                    start_loading(user_id)
                    EXPENSE_SESSIONS[user_id] = {"step": "receipt_processing", "updated_at": time.time(), "pending_text": "", "data": {"registrantUserId": user_id}}
                    data, missing = process_receipt_image(user_id, receipt_base64, receipt_mime)
                except ValueError:
                    EXPENSE_SESSIONS.pop(user_id, None)
                    reply_text(reply_token, "這張圖片目前無法確認為代墊單據，請重新拍攝清楚完整的收據或發票。")
                    continue
                except requests.RequestException:
                    EXPENSE_SESSIONS.pop(user_id, None)
                    reply_text(reply_token, "目前無法辨識單據，請稍後重新上傳。")
                    continue
                data = merge_pending_receipt_text(user_id, data)
                missing = missing_expense_fields(data)
                new_session = {"step": "quick_missing" if missing else "quick_confirm", "updated_at": time.time(), "raw_text": "", "data": data}
                EXPENSE_SESSIONS[user_id] = new_session
                reply_messages(reply_token, [build_project_or_missing_prompt(new_session, missing) if missing else build_expense_confirmation(data)])
                continue

            if session.get("step") not in {"receipt", "quick_confirm", "quick_missing", "quick_edit"}:
                continue
            if session.get("step") == "receipt":
                session["data"]["receiptBase64"] = receipt_base64
                session["data"]["receiptMimeType"] = receipt_mime
                session["step"] = "note"
                reply_messages(reply_token, [next_prompt(session)])
            else:
                previous_data = dict(session.get("data", {}))
                previous_step = session.get("step", "quick_confirm")
                session["step"] = "receipt_processing"
                try:
                    start_loading(user_id)
                    receipt_data, _ = process_receipt_image(user_id, receipt_base64, receipt_mime)
                except (ValueError, requests.RequestException):
                    session["step"] = previous_step
                    reply_text(reply_token, "目前無法辨識單據，原本資料已保留，請重新拍攝後再上傳。")
                    continue
                # 保留員工已明確輸入的欄位，其餘公司統編與單據資訊採用 OCR 結果。
                for field in ("project", "item", "category", "amount", "date", "payer"):
                    if previous_data.get(field) not in (None, ""):
                        receipt_data[field] = previous_data[field]
                if previous_data.get("note"):
                    receipt_data["note"] = previous_data["note"]
                session["data"] = receipt_data
                missing = missing_expense_fields(receipt_data)
                session["step"] = "quick_missing" if missing else "quick_confirm"
                reply_messages(reply_token, [build_project_or_missing_prompt(session, missing) if missing else build_expense_confirmation(receipt_data)])
            continue

        if message_type != "text":
            continue

        text = message.get("text", "").strip()
        command = text.casefold()

        # 三位內部成員可用今日、明日、後天、本週或「行程」查詢指定範圍。
        if is_calendar_command(event):
            try:
                start_loading(user_id)
                reply_messages(reply_token, [calendar_command_message(text=text)])
            except requests.RequestException:
                reply_text(reply_token, "目前無法完整讀取 Google Calendar，請稍後再試；本次沒有將資料顯示為無行程。")
            continue

        session = get_expense_session(user_id) if source.get("type") == "user" else None
        # 圖片正在 OCR 時先把文字併入同一筆，最後只回傳一張完整確認圖卡。
        if session and session.get("step") == "receipt_processing" and (
            "代墊" in command or looks_like_expense_intent(text)
        ):
            session["pending_text"] = f"{session.get('pending_text', '')}，{text}".strip("，")
            session["updated_at"] = time.time()
            continue
        if session and session.get("step") == "stats_project":
            try:
                stats = get_expense_stats(user_id)
                stats["projects"] = [item for item in stats.get("projects", []) if text.casefold() in str(item.get("project", "")).casefold()]
                reply_messages(reply_token, [expense_stats_card(stats)])
            except requests.RequestException:
                reply_text(reply_token, "目前無法讀取專案統計，請稍後再試。")
            EXPENSE_SESSIONS.pop(user_id, None)
            continue
        if session and session.get("step") == "quick_edit" and session.get("edit_field"):
            field = str(session.pop("edit_field"))
            data = session["data"]
            if field == "category":
                data["category"] = text
            elif field == "item":
                data["expenseContent"] = text
                data["item"] = infer_item_option(text) or data.get("item", "")
            elif field == "amount":
                amount = infer_amount(f"{text} 元")
                if amount is None:
                    session["edit_field"] = field
                    reply_text(reply_token, "金額格式不正確，請重新輸入，例如：950。")
                    continue
                data["amount"] = amount
            elif field == "date":
                data["date"] = parse_expense_date(text)
                data["month"] = str(int(data["date"][5:7]))
            session["step"] = "quick_confirm"
            reply_messages(reply_token, [build_expense_confirmation(data)])
            continue
        if session and session.get("step") in {"supplement_project", "supplement_amount"}:
            try:
                if session["step"] == "supplement_project":
                    result = submit_supplement(user_id, int(session["supplement_row"]), project=text)
                else:
                    amount = float(text.replace(",", "").replace("$", ""))
                    if amount <= 0:
                        raise ValueError
                    result = submit_supplement(user_id, int(session["supplement_row"]), amount=amount)
            except (ValueError, requests.RequestException):
                reply_text(reply_token, "補件內容格式不正確或更新失敗，請重新輸入。")
                continue
            EXPENSE_SESSIONS.pop(user_id, None)
            reply_text(reply_token, "補件完成。" if result.get("complete") else "已更新，仍有其他資料需要補件。")
            continue

        if source.get("type") == "user" and user_id in INTERNAL_USER_IDS and looks_like_supplement_query(text):
            try:
                reply_messages(reply_token, [supplement_list_card(get_supplements(user_id))])
            except requests.RequestException:
                reply_text(reply_token, "目前無法讀取待補件資料，請稍後再試。")
            continue

        # 查詢意圖優先於「代墊」關鍵字，絕不建立或修改登記暫存。
        if source.get("type") == "user" and user_id in INTERNAL_USER_IDS and looks_like_expense_query(text):
            try:
                stats = get_expense_stats(user_id)
                reply_messages(reply_token, [expense_stats_card(stats)])
            except requests.RequestException:
                reply_text(reply_token, "目前無法讀取代墊統計，請稍後再試；本次沒有建立代墊紀錄。")
            continue

        # 可直接理解「8月10號外案3萬」等自然語序，再一次補問其餘資料。
        if source.get("type") == "user" and external_case.is_external_case_text(text):
            if user_id not in INTERNAL_USER_IDS:
                reply_text(reply_token, "你的帳號尚未加入公司內部登記名單。")
                continue
            employee_name = EXTERNAL_CASE_NAMES.get(user_id) or get_line_profile_name(user_id)
            session = external_case.start(text, user_id, employee_name)
            reply_messages(reply_token, [external_next_message(session)])
            continue

        external_session = external_case.get_session(user_id) if source.get("type") == "user" else None
        if external_session and external_session.get("step") != "confirm":
            session = external_case.accept_text(user_id, text)
            reply_messages(reply_token, [external_next_message(session)])
            continue

        # 圖片後直接輸入「代墊 PJR 專案」時，同時啟動 OCR 並累積本句資料。
        pending_receipt = get_expense_session(user_id) if source.get("type") == "user" else None
        if command.startswith("代墊") and command != "代墊" and pending_receipt and pending_receipt.get("step") == "receipt_waiting_trigger":
            if user_id not in INTERNAL_USER_IDS:
                reply_text(reply_token, "你的帳號尚未加入公司內部登記名單。")
                continue
            try:
                data, _ = process_receipt_image(
                    user_id,
                    pending_receipt["receiptBase64"],
                    pending_receipt["receiptMimeType"],
                )
                data = merge_expense_text(data, text, user_id)
            except ValueError as error:
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, str(error))
                continue
            except requests.RequestException:
                reply_text(reply_token, "目前無法辨識收據，圖片已保留，請稍後再輸入「代墊」重試。")
                continue
            missing = missing_expense_fields(data)
            session = {
                "step": "quick_missing" if missing else "quick_confirm",
                "updated_at": time.time(),
                "raw_text": text,
                "data": data,
            }
            EXPENSE_SESSIONS[user_id] = session
            reply_messages(reply_token, [build_project_or_missing_prompt(session, missing) if missing else build_expense_confirmation(data)])
            continue

        if command == "代墊":
            if source.get("type") != "user":
                reply_text(reply_token, "代墊登記請在 Bot 個人聊天室使用。")
                continue
            if user_id not in INTERNAL_USER_IDS:
                reply_text(reply_token, "你的帳號尚未加入公司內部登記名單。")
                continue
            session = get_expense_session(user_id)
            if session and session.get("step") == "receipt_waiting_trigger":
                try:
                    data, missing = process_receipt_image(
                        user_id,
                        session["receiptBase64"],
                        session["receiptMimeType"],
                    )
                except ValueError as error:
                    EXPENSE_SESSIONS.pop(user_id, None)
                    reply_text(reply_token, str(error))
                    continue
                except requests.RequestException:
                    reply_text(reply_token, "目前無法辨識收據，圖片已保留，請稍後再輸入「代墊」重試。")
                    continue
                EXPENSE_SESSIONS[user_id] = {
                    "step": "quick_missing" if missing else "quick_confirm",
                    "updated_at": time.time(),
                    "raw_text": "",
                    "data": data,
                }
                reply_messages(reply_token, [build_project_or_missing_prompt(EXPENSE_SESSIONS[user_id], missing) if missing else build_expense_confirmation(data)])
            else:
                EXPENSE_SESSIONS[user_id] = {
                    "step": "receipt_waiting_image",
                    "updated_at": time.time(),
                    "data": {"registrantUserId": user_id},
                }
                reply_text(reply_token, "請上傳一張完整、清楚且避免反光的收據或發票照片，我會自動辨識。")
            continue

        session = get_expense_session(user_id) if source.get("type") == "user" else None
        is_quick_expense = (
            source.get("type") == "user"
            and user_id in INTERNAL_USER_IDS
            and (
                "代墊" in command
                or looks_like_expense_intent(text)
                or (session and session.get("step") in {"quick_missing", "quick_edit"})
            )
        )
        if is_quick_expense:
            # 確認圖卡後再次輸入完整代墊句子，代表開始新的一筆，而不是修改舊草稿。
            if session and session.get("step") == "quick_confirm" and "代墊" in command:
                session = None
            previous_text = session.get("raw_text", "") if session else ""
            combined_text = f"{previous_text}，{text}".strip("，")
            data = merge_expense_text(session.get("data", {}) if session else {}, text, user_id)
            missing = missing_expense_fields(data)
            session = {
                "step": "quick_missing" if missing else "quick_confirm",
                "updated_at": time.time(),
                "raw_text": combined_text,
                "data": data,
            }
            EXPENSE_SESSIONS[user_id] = session
            if missing:
                reply_messages(reply_token, [build_project_or_missing_prompt(session, missing)])
            else:
                reply_messages(reply_token, [build_expense_confirmation(data)])
            continue

        if session:
            step = session["step"]
            if step in {"project", "project_manual", "project_search"}:
                if step == "project_search":
                    candidates = session.get("project_candidates", [])
                    matched = [project for project in candidates if text.casefold() in str(project.get("name", "")).casefold()]
                    if matched:
                        session["project_candidates"] = matched
                        reply_messages(reply_token, [project_candidate_card(matched)])
                        continue
                if text in {"無", "沒有", "無專案", "專案無"}:
                    reply_text(reply_token, "每筆代墊都需要專案名稱，請輸入專案名稱。")
                    continue
                session["data"]["project"] = text
                if step in {"project_manual", "project_search"}:
                    missing = missing_expense_fields(session["data"])
                    session["step"] = "quick_missing" if missing else "quick_confirm"
                    reply_messages(reply_token, [build_project_or_missing_prompt(session, missing) if missing else build_expense_confirmation(session["data"])])
                    continue
                session["step"] = "item"
            elif step == "item":
                item = infer_item_option(text)
                if not item:
                    reply_messages(reply_token, [item_option_card()])
                    continue
                session["data"]["item"] = item
                session["data"]["category"] = PROJECT_EXPENSE_CATEGORY
                session["step"] = "amount"
            elif step == "amount":
                if text in {"無", "沒有", "不知道"}:
                    reply_messages(reply_token, [amount_missing_card()])
                    continue
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
        "release": APP_RELEASE,
        "line_secret_configured": bool(LINE_CHANNEL_SECRET),
        "line_token_configured": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "registry_configured": bool(
            GROUP_REGISTRY_URL and GROUP_REGISTRY_API_KEY
        ),
        "expense_configured": bool(EXPENSE_API_URL and EXPENSE_API_KEY),
        "receipt_vision_configured": bool(GEMINI_API_KEY),
        "quote_webhook_configured": bool(QUOTE_WEBHOOK_URL),
        "calendar_configured": bool(
            GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN and CALENDAR_IDS
        ),
        "internal_user_count": len(INTERNAL_USER_IDS),
        "owner_configured": bool(OWNER_USER_ID),
    }


@app.get("/")
async def root():
    """提供部署狀態說明，不顯示任何敏感憑證。"""
    return {"service": "AURTOR LINE Bot", "status": "running"}
