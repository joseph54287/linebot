"""AURTOR LINE Bot：共用 Webhook、內部 ID 查詢與客戶群組辨識。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RECEIPT_VISION_MODEL = os.environ.get("RECEIPT_VISION_MODEL", "gemini-3.6-flash")
QUOTE_WEBHOOK_URL = os.environ.get(
    "QUOTE_WEBHOOK_URL",
    "https://linebot-bam2.onrender.com/webhook",
).rstrip("/")
QUOTE_OWNER_USER_ID = "Ub983deb79584603885e5b28e9fdf2d5d"
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
logger = logging.getLogger(__name__)

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

# 依 LINE User ID 自動帶入支出人，避免員工每次重複輸入姓名。
INTERNAL_USER_NAMES = {
    "U6c6441cb38102499d1f80d4ea79a53ab": "周暐",
    "Ub983deb79584603885e5b28e9fdf2d5d": "高爾賢",
    "U9478b00702c716685d9d8b021d62d538": "阿全",
}

CATEGORY_KEYWORDS = {
    "案件支出（餐飲、道具、人員...）": [
        "餐", "便當", "飲料", "道具", "演員", "人員", "車馬", "住宿", "場地", "器材費",
        "吊車", "起重", "吊掛", "機具租賃", "高空車", "堆高機",
    ],
    "例行性支出(水電費/房租)": ["水費", "電費", "房租", "瓦斯", "網路費"],
    "人事費用(商業保險費,薪資 ,勞保,健保,勞退）": ["薪資", "勞保", "健保", "勞退", "保險"],
    "工具設備（軟體/硬體）": ["硬碟", "軟體", "訂閱", "電池", "線材", "設備", "鏡頭", "電腦"],
    "會計支出(營業稅,營所稅,申報費)": ["營業稅", "營所稅", "申報", "會計"],
    "公司雜費": ["停車", "油錢", "郵資", "寄件", "鑰匙", "清潔", "文具"],
    "業務開發": ["業務", "招待", "提案", "拜訪"],
    "行銷費用": ["廣告投放", "行銷", "社群", "宣傳"],
}

FIELD_LABELS = {
    "project": ["專案", "案件"],
    "item": ["項目", "品項", "內容"],
    "amount": ["金額", "費用"],
}


def is_quote_event(event: dict[str, Any]) -> bool:
    """只辨識高爾賢個人聊天室內明確定義的報價操作。"""
    source = event.get("source", {})
    if (
        source.get("type") != "user"
        or source.get("userId") != QUOTE_OWNER_USER_ID
    ):
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


def forward_quote_webhook(body: bytes, signature: str) -> bool:
    """原樣轉送 LINE request；失敗不重試，避免重複寄出報價信。"""
    try:
        response = requests.post(
            QUOTE_WEBHOOK_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Line-Signature": signature,
            },
            timeout=20,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        # 不記錄原始內容、簽章或文案，避免敏感資料進入 Render log。
        logger.error("報價 Webhook 轉送失敗：%s", type(error).__name__)
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
    project = extract_labeled_value(text, FIELD_LABELS["project"])
    item = extract_labeled_value(text, FIELD_LABELS["item"])
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
    project, item = infer_project_and_item(text, amount)
    expense_date = parse_expense_date(text)
    data: dict[str, Any] = {
        "registrantUserId": user_id,
        "date": expense_date,
        "month": str(int(expense_date[5:7])),
        "project": project,
        "item": item,
        "amount": amount,
        "category": infer_category(item or text),
        "payer": infer_payer(text, user_id),
        "payment": "已支出",
        "reimbursed": "是" if re.search(r"已領(?:到)?款", text) else "否",
        "invoice": "是" if re.search(r"有統編|含統編|統編發票", text) else "未開",
        "note": "未附收據" if re.search(r"沒收據|無收據", text) else "無",
    }
    return data, missing_expense_fields(data)


def missing_expense_fields(data: dict[str, Any]) -> list[str]:
    """以後端固定規則驗證必要欄位，不直接信任文字解析結果。"""
    required = {
        "project": "專案名稱（沒有專案請寫「專案無」）",
        "item": "消費項目",
        "amount": "金額",
        "category": "項目分類或更清楚的消費內容",
        "payer": "支出人",
    }
    return [label for field, label in required.items() if data.get(field) in {None, ""}]


def build_missing_prompt(missing: list[str]) -> dict[str, Any]:
    """一次列出所有缺漏欄位，避免逐題追問。"""
    return {
        "type": "text",
        "text": "還差以下資訊就能完成：\n- " + "\n- ".join(missing) + "\n\n請用一句話補充即可。",
    }


def looks_like_expense_intent(text: str) -> bool:
    """辨識沒有寫出「代墊」、但語意仍明確是公司支出的句子。"""
    intent_words = ["我先付", "我墊", "幫公司買", "幫公司付", "先幫公司付", "公司支出"]
    return any(word in text for word in intent_words) and infer_amount(text) is not None


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
                {"type": "button", "action": {"type": "postback", "label": "修改", "data": "expense:modify", "displayText": "修改代墊資料"}},
                {"type": "button", "action": {"type": "postback", "label": "重新拍攝", "data": "expense:retake", "displayText": "重新拍攝收據"}},
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


def analyze_receipt_image(image_base64: str, mime_type: str) -> dict[str, Any]:
    """使用 Gemini 讀取台灣發票或收據並回傳固定欄位。"""
    if not GEMINI_API_KEY:
        raise requests.RequestException("receipt vision is not configured")
    prompt = """你是台灣公司支出單據辨識器。只輸出 JSON，不要 Markdown。
辨識這張圖片是否為發票或收據，並輸出：
documentType, merchantName, date(YYYY-MM-DD或空字串), items(字串陣列),
totalAmount(數字或null), invoiceNumber, taxId, hasBusinessTaxId(布林值),
currency(預設TWD), confidence(0到1), warnings(字串陣列), isReceipt(布林值)。
totalAmount 必須是整張單據的應付或實付總額，不可使用統編、發票號碼、日期或交易序號。
看不清楚就留空並在 warnings 說明，不要猜測。"""
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


def receipt_analysis_to_expense(
    analysis: dict[str, Any],
    user_id: str,
    image_base64: str,
    mime_type: str,
) -> tuple[dict[str, Any], list[str]]:
    """驗證影像辨識結果並轉成既有 Google Sheet 支出欄位。"""
    if not analysis.get("isReceipt"):
        raise ValueError("圖片不像發票或收據，請重新拍攝清楚的完整單據。")

    raw_amount = analysis.get("totalAmount")
    try:
        amount = float(str(raw_amount).replace(",", ""))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        amount = None

    raw_date = str(analysis.get("date") or "").strip()
    try:
        expense_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
    except ValueError:
        expense_date = ""

    items = analysis.get("items") if isinstance(analysis.get("items"), list) else []
    items = [str(item).strip() for item in items if str(item).strip()]
    merchant = str(analysis.get("merchantName") or "").strip()
    item = "、".join(items[:8]) or merchant
    category = infer_category(item)
    invoice_number = str(analysis.get("invoiceNumber") or "").strip()
    tax_id = str(analysis.get("taxId") or "").strip()
    warnings = analysis.get("warnings") if isinstance(analysis.get("warnings"), list) else []
    confidence = float(analysis.get("confidence") or 0)
    note_parts = [part for part in [f"商家：{merchant}" if merchant else "", f"發票號碼：{invoice_number}" if invoice_number else "", f"統編：{tax_id}" if tax_id else ""] if part]
    if confidence < 0.75:
        note_parts.append("影像辨識信心較低，已要求人工確認")
    if warnings:
        note_parts.append("辨識提醒：" + "；".join(map(str, warnings[:3])))

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
        "invoice": "是" if analysis.get("hasBusinessTaxId") else "未開",
        "note": "；".join(note_parts) or "影像收據自動辨識",
        "receiptBase64": image_base64,
        "receiptMimeType": mime_type,
        "receiptDocumentType": str(analysis.get("documentType") or "單據"),
        "receiptConfidence": confidence,
    }
    missing = missing_expense_fields(data)
    return data, missing


def process_receipt_image(user_id: str, image_base64: str, mime_type: str) -> tuple[dict[str, Any], list[str]]:
    """完成收據辨識與欄位轉換，供 Webhook 與測試共用。"""
    analysis = analyze_receipt_image(image_base64, mime_type)
    return receipt_analysis_to_expense(analysis, user_id, image_base64, mime_type)


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
                if session.get("submitting"):
                    reply_text(reply_token, "這筆資料正在送出，請勿重複點擊。")
                    continue
                session["submitting"] = True
                try:
                    session["data"]["registrantName"] = get_line_profile_name(user_id)
                    submit_expense(session["data"])
                except requests.RequestException:
                    session["submitting"] = False
                    reply_text(reply_token, "目前無法寫入支出資料，內容已保留，請稍後再按一次「確認送出」。")
                    continue
                EXPENSE_SESSIONS.pop(user_id, None)
                reply_text(reply_token, "代墊登記完成，資料已寫入公司支出簿。")
                continue
            elif action == "modify":
                session["step"] = "quick_edit"
                reply_text(reply_token, "請直接說要修改的內容，例如：「金額改成 950，發票改為有統編」。")
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

            # 已先輸入「代墊」時，收到圖片就立即進行辨識。
            if session and session.get("step") == "receipt_waiting_image":
                try:
                    data, missing = process_receipt_image(user_id, receipt_base64, receipt_mime)
                except ValueError as error:
                    reply_text(reply_token, str(error))
                    continue
                except requests.RequestException:
                    session["receiptBase64"] = receipt_base64
                    session["receiptMimeType"] = receipt_mime
                    reply_text(reply_token, "目前無法辨識收據，圖片已保留，請稍後再輸入「代墊」重試。")
                    continue
                EXPENSE_SESSIONS[user_id] = {
                    "step": "quick_missing" if missing else "quick_confirm",
                    "updated_at": time.time(),
                    "raw_text": "",
                    "data": data,
                }
                reply_messages(reply_token, [build_missing_prompt(missing) if missing else build_expense_confirmation(data)])
                continue

            # 員工先上傳圖片時先保留，等收到「代墊」才傳送至辨識服務。
            if not session:
                EXPENSE_SESSIONS[user_id] = {
                    "step": "receipt_waiting_trigger",
                    "updated_at": time.time(),
                    "receiptBase64": receipt_base64,
                    "receiptMimeType": receipt_mime,
                    "data": {"registrantUserId": user_id},
                }
                reply_text(reply_token, "已收到單據照片。請輸入「代墊」，我會自動辨識並填寫。")
                continue

            if session.get("step") not in {"receipt", "quick_confirm", "quick_missing", "quick_edit"}:
                continue
            session["data"]["receiptBase64"] = receipt_base64
            session["data"]["receiptMimeType"] = receipt_mime
            if session.get("step") == "receipt":
                session["step"] = "note"
                reply_messages(reply_token, [next_prompt(session)])
            else:
                session["data"]["note"] = "已附收據"
                session["step"] = "quick_confirm"
                reply_messages(reply_token, [build_expense_confirmation(session["data"])])
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
                reply_messages(reply_token, [build_missing_prompt(missing) if missing else build_expense_confirmation(data)])
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
            previous_text = session.get("raw_text", "") if session else ""
            combined_text = f"{previous_text}，{text}".strip("，")
            data, missing = parse_expense_text(combined_text, user_id)

            # 修改或補充時保留先前已確認的欄位，只覆蓋本次能明確解析的內容。
            if session and session.get("data"):
                previous_data = session["data"]
                for field, value in previous_data.items():
                    if field not in data or data.get(field) in {None, ""}:
                        data[field] = value
            missing = missing_expense_fields(data)
            session = {
                "step": "quick_missing" if missing else "quick_confirm",
                "updated_at": time.time(),
                "raw_text": combined_text,
                "data": data,
            }
            EXPENSE_SESSIONS[user_id] = session
            if missing:
                reply_messages(reply_token, [build_missing_prompt(missing)])
            else:
                reply_messages(reply_token, [build_expense_confirmation(data)])
            continue

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
        "receipt_vision_configured": bool(GEMINI_API_KEY),
        "quote_webhook_configured": bool(QUOTE_WEBHOOK_URL),
        "internal_user_count": len(INTERNAL_USER_IDS),
        "owner_configured": bool(OWNER_USER_ID),
    }


@app.get("/")
async def root():
    """提供部署狀態說明，不顯示任何敏感憑證。"""
    return {"service": "AURTOR LINE Bot", "status": "running"}
