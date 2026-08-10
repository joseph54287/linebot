"""LINE 外案申請：自然語言補問、主管核准與試算表歸檔。"""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
SESSION_TTL = 30 * 60
SESSIONS: dict[str, dict[str, Any]] = {}

CASE_TYPES = ["導演案", "剪接案", "製片案", "其他"]
DESTINATIONS = ["公司", "員工個人", "尚未確認"]
DETAILS_PROMPT = (
    "金額收到，請再補這五項\n\n"
    "案名：\n"
    "案型：導演案／剪接案／製片案／其他\n"
    "款項進入：公司／員工個人／尚未確認\n"
    "預計匯款日：\n"
    "聯繫窗口："
)


def is_external_case_text(text: str) -> bool:
    return "外案" in text


def parse_date(text: str, now: datetime | None = None) -> str:
    today = (now or datetime.now(TAIPEI)).date()
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\u200b-\u200d\ufeff]", "", normalized)
    match = re.search(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*(?:月|[/.-])\s*(\d{1,2})\s*(?:日|號)?", normalized)
    if match:
        year, month, day = match.groups()
    else:
        chinese = re.search(r"([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*(?:日|號)?", normalized)
        if not chinese:
            return ""

        def chinese_number(token: str) -> int:
            digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
            if token == "十":
                return 10
            if "十" in token:
                left, right = token.split("十", 1)
                return digits.get(left, 1) * 10 + digits.get(right, 0)
            return digits.get(token, 0)

        year = None
        month, day = map(chinese_number, chinese.groups())
    try:
        return datetime(int(year or today.year), int(month), int(day)).date().isoformat()
    except ValueError:
        return ""


def parse_amount(text: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\u200b-\u200d\ufeff]", "", normalized)
    normalized = normalized.replace("，", ",")
    match = re.search(r"(?:NT\$|\$)?\s*([\d,]+(?:\.\d+)?)\s*萬", normalized, re.I)
    if match:
        value = float(match.group(1).replace(",", "")) * 10_000
        return int(value) if value > 0 and value.is_integer() else None
    match = re.search(r"(?:NT\$|\$)?\s*([\d,]+(?:\.\d+)?)\s*(?:千|[kK])", normalized)
    if match:
        value = float(match.group(1).replace(",", "")) * 1_000
        return int(value) if value > 0 and value.is_integer() else None
    colloquial_ten_thousand = re.search(r"([一二兩三四五六七八九])\s*萬\s*([一二三四五六七八九])", normalized)
    if colloquial_ten_thousand:
        digits = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        return digits[colloquial_ten_thousand.group(1)] * 10_000 + digits[colloquial_ten_thousand.group(2)] * 1_000
    chinese_ten_thousand = re.search(r"(?:^|\D)(十|[一二兩三四五六七八九]\s*十?|十\s*[一二三四五六七八九])\s*萬", normalized)
    if chinese_ten_thousand:
        token = re.sub(r"\s+", "", chinese_ten_thousand.group(1))
        digits = {"一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if token == "十":
            units = 10
        elif "十" in token:
            left, right = token.split("十", 1)
            units = digits.get(left, 1) * 10 + digits.get(right, 0)
        else:
            units = digits.get(token, 0)
        return units * 10_000 if units > 0 else None
    match = re.search(r"(?:金額\s*[：:]?\s*|NT\$|\$)\s*([\d,]+)|([\d,]+)\s*(?:元|塊)", normalized, re.I)
    if match:
        raw = next((part for part in match.groups() if part), "")
        value = int(raw.replace(",", ""))
        return value if value > 0 else None
    # 員工也會直接輸入「外案 500」或「外案 100000」；移除日期後取最後一個純數字。
    if "外案" not in normalized:
        return None
    tail = normalized.split("外案", 1)[1]
    tail = re.sub(r"(?:(?:20\d{2})\s*年\s*)?\d{1,2}\s*(?:月|[/.-])\s*\d{1,2}\s*(?:日|號)?", " ", tail)
    bare_values = re.findall(r"(?<![\d/.-])([\d,]+)(?![\d/.-])", tail)
    if not bare_values:
        return None
    raw = bare_values[-1]
    value = int(raw.replace(",", ""))
    return value if value > 0 else None


def infer_case_type(text: str) -> str:
    for keyword, value in [
        ("導演", "導演案"), ("剪接", "剪接案"), ("剪片", "剪接案"),
        ("後期", "剪接案"), ("剪輯", "剪接案"), ("製片", "製片案"),
    ]:
        if keyword in text:
            return value
    return ""


def parse_payment_date(text: str, now: datetime | None = None) -> str:
    match = re.search(r"(?:預計)?匯款(?:日期|日)?\s*[：:]?\s*([^\n，,]+)", text)
    return parse_date(match.group(1), now) if match else ""


def parse_initial(text: str, user_id: str, employee_name: str) -> dict[str, Any]:
    entered_amount = parse_amount(text)
    tax_mode = "含稅" if re.search(r"含\s*稅", unicodedata.normalize("NFKC", text)) else "稅外"
    pretax_amount = round(entered_amount / 1.05) if entered_amount and tax_mode == "含稅" else entered_amount
    return {
        "requestId": str(uuid.uuid4()),
        "employeeUserId": user_id,
        "employeeName": employee_name,
        "date": parse_date(text),
        "amount": pretax_amount,
        "enteredAmount": entered_amount,
        "taxMode": tax_mode,
        "caseType": infer_case_type(text),
        "projectName": "",
        "destination": "",
        "paymentDate": parse_payment_date(text),
        "contact": "",
    }


def get_session(user_id: str) -> dict[str, Any] | None:
    session = SESSIONS.get(user_id)
    if session and time.time() - session["updatedAt"] <= SESSION_TTL:
        return session
    SESSIONS.pop(user_id, None)
    return None


def next_step(data: dict[str, Any]) -> str:
    for field in ["date", "amount"]:
        if not data.get(field):
            return field
    if any(not data.get(field) for field in ["projectName", "caseType", "destination", "paymentDate", "contact"]):
        return "details"
    return "confirm"


def prompt(step: str) -> dict[str, Any]:
    texts = {
        "projectName": "這個案子叫什麼？",
        "date": "哪一天？例如：8/10",
        "amount": "金額是多少？",
    }
    if step in texts:
        return {"type": "text", "text": texts[step]}
    if step == "details":
        return {"type": "text", "text": DETAILS_PROMPT}
    options = CASE_TYPES if step == "caseType" else DESTINATIONS
    title = "這是什麼類型的案子？" if step == "caseType" else "客戶的款項會匯到哪裡？"
    return {
        "type": "template", "altText": title,
        "template": {"type": "buttons", "title": "外案", "text": title, "actions": [
            {"type": "postback", "label": value, "data": f"external:set:{step}:{value}", "displayText": value}
            for value in options
        ]},
    }


def money(value: int) -> str:
    return f"${value:,}"


def tax_amounts(data: dict[str, Any]) -> tuple[int, int, int]:
    pretax = int(data["amount"])
    gross = int(data.get("enteredAmount") or 0) if data.get("taxMode") == "含稅" else round(pretax * 1.05)
    return pretax, gross - pretax, gross


def parse_details(text: str) -> dict[str, str]:
    result = {"projectName": "", "caseType": infer_case_type(text), "destination": "", "paymentDate": parse_payment_date(text), "contact": ""}
    project = re.search(r"(?:案名|專案名稱|專案)\s*[：:]\s*([^\n]+)", text)
    case_type = re.search(r"案型\s*[：:]\s*([^\n]+)", text)
    destination = re.search(r"(?:款項(?:進入|匯入)?|入帳)\s*[：:]\s*([^\n]+)", text)
    contact = re.search(r"(?:(?:聯繫|聯絡)窗口|窗口)\s*[：:]\s*([^\n]+)", text)
    if project:
        result["projectName"] = project.group(1).strip()
    if case_type:
        value = case_type.group(1).strip()
        result["caseType"] = infer_case_type(value) or value
    destination_text = destination.group(1).strip() if destination else text
    if "員工個人" in destination_text or "個人帳戶" in destination_text:
        result["destination"] = "員工個人"
    elif "尚未確認" in destination_text or "還不知道" in destination_text:
        result["destination"] = "尚未確認"
    elif "公司" in destination_text:
        result["destination"] = "公司"
    if contact:
        result["contact"] = contact.group(1).strip()
    return result


def confirmation_card(data: dict[str, Any]) -> dict[str, Any]:
    pretax, tax, gross = tax_amounts(data)
    employee_share = round(data["amount"] * 0.4)
    company_share = data["amount"] - employee_share
    rows = [
        ("案名", data["projectName"]), ("案件日期", data["date"]), ("案型", data["caseType"]),
        ("計價方式", data.get("taxMode", "稅外")), ("未稅金額", money(pretax)),
        ("稅額", money(tax)), ("含稅收款", money(gross)), ("款項進入", data["destination"]),
        ("預計匯款日", data["paymentDate"]), ("聯繫窗口", data["contact"]),
        ("你的獎金 40%", money(employee_share)), ("公司 60%", money(company_share)),
    ]
    body: list[dict[str, Any]] = []
    for label, value in rows:
        body.append({"type": "text", "text": f"{label}：{value}", "size": "sm", "color": "#1F2937", "wrap": True, "margin": "md"})
    return {"type": "flex", "altText": "請最後確認外案資料", "contents": {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#193B65", "paddingAll": "20px", "contents": [
            {"type": "text", "text": "最後確認", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
            {"type": "text", "text": "確認後才會送進公司營運系統", "color": "#DCE7F5", "size": "xs", "margin": "sm"},
        ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{
            "type": "button", "style": "primary", "color": "#193B65",
            "action": {"type": "postback", "label": "確認送出", "data": "external:submit", "displayText": "確認送出"},
        }]},
    }}


def approval_card(data: dict[str, Any]) -> dict[str, Any]:
    pretax, tax, gross = tax_amounts(data)
    employee_share = round(data["amount"] * 0.4)
    company_share = data["amount"] - employee_share
    request_id = data["requestId"]
    rows = [
        ("申請人", data["employeeName"]), ("案名", data["projectName"]), ("案件日期", data["date"]),
        ("案型", data["caseType"]), ("未稅金額", money(pretax)), ("稅額", money(tax)),
        ("含稅收款", money(gross)), ("款項進入", data["destination"]),
        ("員工 40%", money(employee_share)), ("公司 60%", money(company_share)),
        ("預計匯款日", data["paymentDate"]), ("聯繫窗口", data["contact"]),
    ]
    body = [{"type": "text", "text": f"{label}：{value}", "size": "sm", "color": "#1F2937", "wrap": True, "margin": "md"} for label, value in rows]
    return {"type": "flex", "altText": f"{data['employeeName']}送出外案申請，等待核准", "contents": {
        "type": "bubble",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": "#193B65", "paddingAll": "20px", "contents": [
            {"type": "text", "text": "外案待核准", "color": "#FFFFFF", "size": "xl", "weight": "bold"},
            {"type": "text", "text": "請選擇確認成立或待討論", "color": "#DCE7F5", "size": "xs", "margin": "sm"},
        ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": body},
        "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "20px", "contents": [
            {"type": "button", "style": "primary", "color": "#193B65", "action": {"type": "message", "label": "確認成立", "text": f"外案核准:{request_id}"}},
            {"type": "button", "style": "secondary", "action": {"type": "message", "label": "待討論", "text": f"外案待討論:{request_id}"}},
        ]},
    }}


def api_call(api_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not api_url or not api_key:
        raise requests.RequestException("bonus API is not configured")
    response = requests.post(api_url, params={"key": api_key}, json=payload, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise requests.RequestException("bonus API rejected request")
    return result


def start(text: str, user_id: str, employee_name: str) -> dict[str, Any]:
    data = parse_initial(text, user_id, employee_name)
    session = {"data": data, "step": next_step(data), "updatedAt": time.time()}
    SESSIONS[user_id] = session
    return session


def accept_text(user_id: str, text: str) -> dict[str, Any] | None:
    session = get_session(user_id)
    if not session:
        return None
    data, step = session["data"], session["step"]
    if step == "projectName":
        data[step] = text.strip()
    elif step == "date":
        data[step] = parse_date(text)
    elif step == "amount":
        entered_amount = parse_amount(text)
        data["enteredAmount"] = entered_amount
        data["taxMode"] = "含稅" if "含稅" in text else "稅外"
        data[step] = round(entered_amount / 1.05) if entered_amount and data["taxMode"] == "含稅" else entered_amount
    elif step == "details":
        for field, value in parse_details(text).items():
            if value:
                data[field] = value
    elif step == "caseType":
        data[step] = infer_case_type(text) or text.strip()
    elif step == "destination" and text in DESTINATIONS:
        data[step] = text
    if step == "details":
        session["step"] = next_step(data)
        session["updatedAt"] = time.time()
        return session
    if not data.get(step):
        return session
    session["step"] = next_step(data)
    session["updatedAt"] = time.time()
    return session


def accept_option(user_id: str, field: str, value: str) -> dict[str, Any] | None:
    session = get_session(user_id)
    if not session or field not in {"caseType", "destination"}:
        return None
    session["data"][field] = value
    session["step"] = next_step(session["data"])
    session["updatedAt"] = time.time()
    return session
