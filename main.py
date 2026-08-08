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
from datetime import datetime, timedelta
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RECEIPT_VISION_MODEL = os.environ.get("RECEIPT_VISION_MODEL", "gemini-3.6-flash")
PROJECT_API_URL = os.environ.get("PROJECT_API_URL", "").rstrip("/")
PROJECT_API_KEY = os.environ.get("PROJECT_API_KEY", "")
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
LOGGER = logging.getLogger("aurtor-line-bot")

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


def project_candidate_card(projects: list[dict[str, Any]]) -> dict[str, Any]:
    """建立近期未結案專案圖卡；postback 只保存索引，避免長名稱超限。"""
    buttons = [
        {
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "margin": "sm",
            "action": {
                "type": "postback",
                "label": str(project["name"])[:20],
                "data": f"expense:project:{index}",
                "displayText": str(project["name"]),
            },
        }
        for index, project in enumerate(projects)
    ]
    buttons.append({
        "type": "button",
        "height": "sm",
        "margin": "sm",
        "action": {
            "type": "postback",
            "label": "其他／自行輸入",
            "data": "expense:project:manual",
            "displayText": "自行輸入專案名稱",
        },
    })
    return {
        "type": "flex",
        "altText": "請選擇近期未結案專案",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#17365D",
                "contents": [{"type": "text", "text": "選擇專案", "color": "#FFFFFF", "weight": "bold"}],
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
    """篩選最近 90 天未結案專案，並依內容相關性與更新時間排序。"""
    current = (now or datetime.now(TAIPEI_TZ)).replace(tzinfo=None)
    cutoff = current - timedelta(days=90)
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
        if updated_at and updated_at < cutoff:
            continue
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
    return [project for _, _, project in candidates[:10]]


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
    """專案缺漏時優先提供近期選項；API 無法使用則回到一次性文字補充。"""
    project_label = "專案名稱（沒有專案請寫「專案無」）"
    if project_label not in missing:
        return build_missing_prompt(missing)
    context = " ".join([
        str(session.get("raw_text") or ""),
        str(session.get("data", {}).get("item") or ""),
        str(session.get("data", {}).get("note") or ""),
    ])
    try:
        projects = get_recent_open_projects(context)
    except (requests.RequestException, ValueError, TypeError):
        projects = []
    if not projects:
        return build_missing_prompt(missing)
    session["project_candidates"] = projects
    LOGGER.info("expense project candidates count=%s", len(projects))
    return project_candidate_card(projects)


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


def analyze_receipt_image(image_base64: str, mime_type: str, focused_retry: bool = False) -> dict[str, Any]:
    """使用 Gemini 讀取台灣發票或收據；必要時執行聚焦總額的第二輪。"""
    if not GEMINI_API_KEY:
        raise requests.RequestException("receipt vision is not configured")
    prompt = """你是台灣公司支出單據 OCR 欄位擷取器。只輸出 JSON，不要 Markdown。
不要因為照片角度、皺摺、手寫、裁切或版型陌生就拒絕，請先盡力擷取可見欄位。輸出：
documentType, merchantName, date(YYYY-MM-DD或空字串), items(字串陣列),
totalAmount(數字或null), invoiceNumber, taxId, hasBusinessTaxId(布林值),
currency(預設TWD), confidence(0到1), warnings(字串陣列), isReceipt(布林值), imageType(簡短字串)。
totalAmount 必須是整張單據的應付或實付總額，不可使用統編、發票號碼、日期或交易序號。
看不清楚就留空並在 warnings 說明，不要猜測。"""
    if focused_retry:
        prompt += """
這是第二輪校對。請像 OCR 人員逐行查看「總計、合計、應付、實付、現金、信用卡、TOTAL」附近數字，
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
    for field in ["documentType", "merchantName", "date", "items", "totalAmount", "invoiceNumber", "taxId", "currency", "imageType"]:
        current_value = merged.get(field)
        if current_value is None or current_value == "" or (field == "items" and not current_value):
            merged[field] = retry.get(field)
    if valid_receipt_amount(primary) is None and valid_receipt_amount(retry) is not None:
        merged["totalAmount"] = retry["totalAmount"]
    merged["hasBusinessTaxId"] = bool(primary.get("hasBusinessTaxId") or retry.get("hasBusinessTaxId"))
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
        "receiptSecondPass": bool(analysis.get("usedSecondPass")),
    }
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
    if valid_receipt_amount(analysis) is None or receipt_signal_count(analysis) == 0:
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
            if action == "project":
                if value == "manual":
                    session["step"] = "project_manual"
                    reply_text(reply_token, "請直接輸入專案名稱；沒有專案請輸入「專案無」。")
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
                reply_messages(reply_token, [build_missing_prompt(missing) if missing else build_expense_confirmation(session["data"])])
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
                reply_messages(reply_token, [build_project_or_missing_prompt(EXPENSE_SESSIONS[user_id], missing) if missing else build_expense_confirmation(data)])
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
                reply_messages(reply_token, [build_project_or_missing_prompt(session, missing)])
            else:
                reply_messages(reply_token, [build_expense_confirmation(data)])
            continue

        if session:
            step = session["step"]
            if step in {"project", "project_manual"}:
                session["data"]["project"] = text
                if step == "project_manual":
                    missing = missing_expense_fields(session["data"])
                    session["step"] = "quick_missing" if missing else "quick_confirm"
                    reply_messages(reply_token, [build_missing_prompt(missing) if missing else build_expense_confirmation(session["data"])])
                    continue
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
        "internal_user_count": len(INTERNAL_USER_IDS),
        "owner_configured": bool(OWNER_USER_ID),
    }


@app.get("/")
async def root():
    """提供部署狀態說明，不顯示任何敏感憑證。"""
    return {"service": "AURTOR LINE Bot", "status": "running"}
