"""LINE 外案申請：自然語言補問、主管核准與試算表歸檔。"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

TAIPEI = ZoneInfo("Asia/Taipei")
SESSION_TTL = 30 * 60
SESSIONS: dict[str, dict[str, Any]] = {}

CASE_TYPES = ["拍攝", "剪輯", "拍攝＋剪輯", "其他"]
DESTINATIONS = ["公司", "員工個人", "尚未確認"]


def is_external_case_text(text: str) -> bool:
    return text.strip().startswith("外案")


def parse_date(text: str, now: datetime | None = None) -> str:
    today = (now or datetime.now(TAIPEI)).date()
    match = re.search(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*(?:月|[/.-])\s*(\d{1,2})\s*(?:日|號)?", text)
    if not match:
        return ""
    year, month, day = match.groups()
    try:
        return datetime(int(year or today.year), int(month), int(day)).date().isoformat()
    except ValueError:
        return ""


def parse_amount(text: str) -> int | None:
    match = re.search(r"(?:NT\$|[$＄])?\s*(\d+(?:\.\d+)?)\s*萬", text, re.I)
    if match:
        value = float(match.group(1)) * 10_000
        return int(value) if value > 0 and value.is_integer() else None
    match = re.search(r"(?:金額\s*[：:]?\s*|NT\$|[$＄])\s*([\d,]+)|([\d,]+)\s*(?:元|塊)", text, re.I)
    if not match:
        return None
    raw = next((part for part in match.groups() if part), "")
    value = int(raw.replace(",", ""))
    return value if value > 0 else None


def infer_case_type(text: str) -> str:
    if "拍攝" in text and "剪輯" in text:
        return "拍攝＋剪輯"
    for keyword, value in [("剪片", "剪輯"), ("後期", "剪輯"), ("剪輯", "剪輯"), ("攝影", "拍攝"), ("拍攝", "拍攝")]:
        if keyword in text:
            return value
    return ""


def parse_initial(text: str, user_id: str, employee_name: str) -> dict[str, Any]:
    return {
        "requestId": str(uuid.uuid4()),
        "employeeUserId": user_id,
        "employeeName": employee_name,
        "date": parse_date(text),
        "amount": parse_amount(text),
        "caseType": infer_case_type(text),
        "projectName": "",
        "destination": "",
    }


def get_session(user_id: str) -> dict[str, Any] | None:
    session = SESSIONS.get(user_id)
    if session and time.time() - session["updatedAt"] <= SESSION_TTL:
        return session
    SESSIONS.pop(user_id, None)
    return None


def next_step(data: dict[str, Any]) -> str:
    for field in ["projectName", "date", "amount", "caseType", "destination"]:
        if not data.get(field):
            return field
    return "confirm"


def prompt(step: str) -> dict[str, Any]:
    texts = {
        "projectName": "這個案子叫什麼？",
        "date": "哪一天？例如：8/10",
        "amount": "未稅金額是多少？",
    }
    if step in texts:
        return {"type": "text", "text": texts[step]}
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


def confirmation_card(data: dict[str, Any]) -> dict[str, Any]:
    employee_share = round(data["amount"] * 0.4)
    company_share = data["amount"] - employee_share
    summary = "\n".join([
        data["projectName"], f"日期：{data['date']}", f"未稅金額：{money(data['amount'])}",
        f"案型：{data['caseType']}", f"款項匯入：{data['destination']}", "",
        f"你應得：{money(employee_share)}", f"公司應得：{money(company_share)}",
    ])
    return {"type": "template", "altText": "確認外案申請", "template": {
        "type": "buttons", "title": "確認外案申請", "text": summary[:160], "actions": [
            {"type": "postback", "label": "送出核准", "data": "external:submit", "displayText": "送出核准"},
            {"type": "postback", "label": "取消", "data": "external:cancel", "displayText": "取消外案申請"},
        ],
    }}


def approval_card(data: dict[str, Any]) -> dict[str, Any]:
    employee_share = round(data["amount"] * 0.4)
    company_share = data["amount"] - employee_share
    summary = "\n".join([
        f"{data['employeeName']}｜{data['projectName']}", f"日期：{data['date']}",
        f"未稅金額：{money(data['amount'])}", f"案型：{data['caseType']}",
        f"款項匯入：{data['destination']}", f"員工 40%：{money(employee_share)}", f"公司 60%：{money(company_share)}",
    ])
    request_id = data["requestId"]
    return {"type": "template", "altText": f"{data['employeeName']}送出外案申請", "template": {
        "type": "buttons", "title": "外案待核准", "text": summary[:160], "actions": [
            {"type": "postback", "label": "核准並登記", "data": f"external:approve:{request_id}", "displayText": "核准外案"},
            {"type": "postback", "label": "拒絕", "data": f"external:reject:{request_id}", "displayText": "拒絕外案"},
        ],
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
        data[step] = parse_amount(text)
    elif step == "caseType":
        data[step] = infer_case_type(text) or text.strip()
    elif step == "destination" and text in DESTINATIONS:
        data[step] = text
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
