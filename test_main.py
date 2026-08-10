"""代墊登記核心流程測試。"""

import asyncio
import json

import pytest

import main
import external_case


def test_attendance_distance_and_location_prompt():
    assert main.attendance_distance_meters(25.0, 121.5, 25.0, 121.5) == 0
    assert 90 < main.attendance_distance_meters(25.0, 121.5, 25.0009, 121.5) < 110
    prompt = main.attendance_location_prompt("上班打卡")
    assert prompt["quickReply"]["items"][0]["action"]["type"] == "location"


def test_attendance_location_records_success(monkeypatch):
    user_id = main.QUOTE_OWNER_USER_ID
    calls = []
    main.ATTENDANCE_SESSIONS[user_id] = {"mode": "attendance", "type": "上班打卡", "updated_at": main.time.time()}

    def fake_api(action, payload=None):
        calls.append((action, payload))
        if action == "attendance_config_get":
            return {"ok": True, "config": {"configured": True, "latitude": 25.0, "longitude": 121.5, "radiusMeters": 200, "address": "公司"}}
        return {"ok": True, "row": 2}

    monkeypatch.setattr(main, "attendance_api", fake_api)
    result = main.process_attendance_location(user_id, {"latitude": 25.0005, "longitude": 121.5, "address": "新店"}, "evt-1")
    assert "上班打卡成功" in result
    assert [item[0] for item in calls] == ["attendance_config_get", "attendance_record"]
    assert calls[1][1]["attendance"]["withinRange"] is True
    assert calls[1][1]["attendance"]["transactionId"] == "evt-1"


def test_attendance_location_records_out_of_range(monkeypatch):
    user_id = main.QUOTE_OWNER_USER_ID
    recorded = {}
    main.ATTENDANCE_SESSIONS[user_id] = {"mode": "attendance", "type": "下班打卡", "updated_at": main.time.time()}

    def fake_api(action, payload=None):
        if action == "attendance_config_get":
            return {"ok": True, "config": {"configured": True, "latitude": 25.0, "longitude": 121.5, "radiusMeters": 200}}
        recorded.update(payload["attendance"])
        return {"ok": True}

    monkeypatch.setattr(main, "attendance_api", fake_api)
    result = main.process_attendance_location(user_id, {"latitude": 25.01, "longitude": 121.5}, "evt-2")
    assert "不在允許範圍" in result
    assert recorded["withinRange"] is False


def test_liff_attendance_uses_verified_user_and_server_time(monkeypatch):
    calls = []

    def fake_api(action, payload=None):
        calls.append((action, payload))
        if action == "attendance_config_get":
            return {"ok": True, "config": {"configured": True, "latitude": 25.0, "longitude": 121.5, "radiusMeters": 200, "address": "公司"}}
        return {"ok": True, "row": 2}

    monkeypatch.setattr(main, "attendance_api", fake_api)
    result = main.record_liff_attendance(main.QUOTE_OWNER_USER_ID, "clock_in", 25.0005, 121.5, 12.3)
    assert result["success"] is True
    record = calls[1][1]["attendance"]
    assert record["userId"] == main.QUOTE_OWNER_USER_ID
    assert record["type"] == "上班打卡"
    assert record["accuracyMeters"] == 12.3
    assert record["source"] == "LIFF"
    assert record["recordedAt"].endswith("+08:00")


def test_liff_attendance_rejects_unknown_user():
    try:
        main.record_liff_attendance("U-unknown", "clock_in", 25.0, 121.5, 10)
    except PermissionError:
        pass
    else:
        raise AssertionError("unknown LINE user must be rejected")


def test_calendar_command_merges_two_days_into_blue_cards(monkeypatch):
    calls = []
    today_event = {
        "summary": "AI 課程",
        "start": {"dateTime": "2026-08-09T10:00:00+08:00"},
        "end": {"dateTime": "2026-08-09T12:00:00+08:00"},
        "location": "台北",
        "htmlLink": "https://calendar.google.com/event?eid=today",
    }

    def fake_fetch(start, end):
        calls.append((start, end))
        return [today_event] if len(calls) == 1 else []

    monkeypatch.setattr(main, "fetch_calendar_events", fake_fetch)
    message = main.calendar_command_message(
        main.datetime(2026, 8, 9, 18, 0, tzinfo=main.TAIPEI_TZ)
    )
    assert len(calls) == 2
    assert message["altText"] == "今日與明日行程"
    today, tomorrow = message["contents"]["contents"]
    assert today["header"]["contents"][0]["text"] == "今日行程"
    assert today["header"]["backgroundColor"] == "#2563EB"
    assert today["body"]["contents"][0]["contents"][0]["text"] == "AI 課程"
    assert today["body"]["contents"][0]["contents"][1]["text"] == "10:00–12:00｜台北"
    assert tomorrow["header"]["contents"][0]["text"] == "明日行程"
    assert tomorrow["body"]["contents"][0]["text"] == "沒有行程"


def test_calendar_fetch_deduplicates_identical_events(monkeypatch):
    monkeypatch.setattr(main, "google_access_token", lambda: "access-token")
    event = {
        "summary": "公司會議",
        "start": {"dateTime": "2026-08-09T13:00:00+08:00"},
        "end": {"dateTime": "2026-08-09T14:00:00+08:00"},
        "location": "辦公室",
    }

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [event]}

    calls = []
    monkeypatch.setattr(main.requests, "get", lambda *args, **kwargs: calls.append((args, kwargs)) or Response())
    start = main.datetime(2026, 8, 9, tzinfo=main.TAIPEI_TZ)
    events = main.fetch_calendar_events(start, start + main.timedelta(days=1))
    assert len(calls) == 2
    assert events == [event]


def test_calendar_command_requires_exact_keyword_and_internal_user():
    event = {
        "type": "message",
        "source": {"type": "user", "userId": main.QUOTE_OWNER_USER_ID},
        "message": {"type": "text", "text": "行程"},
    }
    assert main.is_calendar_command(event) is True
    for user_id in main.INTERNAL_USER_IDS:
        event["source"]["userId"] = user_id
        assert main.is_calendar_command(event) is True
    for text in ["今日行程", "今天的行程", "明天行程", "明日的行程", "後天行程", "這週的行程", "本週行程"]:
        event["message"]["text"] = text
        assert main.is_calendar_command(event) is True
    event["message"]["text"] = "下週行程"
    assert main.is_calendar_command(event) is False
    event["message"]["text"] = "行程"
    event["source"]["userId"] = "U-other"
    assert main.is_calendar_command(event) is False
    event["source"] = {"type": "group", "userId": main.QUOTE_OWNER_USER_ID}
    assert main.is_calendar_command(event) is False


def test_calendar_keyword_intents_ignore_particle_and_spaces():
    assert main.calendar_query_intent("請給我今日行程") == "today"
    assert main.calendar_query_intent("今天 的 行程") == "today"
    assert main.calendar_query_intent("明天的行程") == "tomorrow"
    assert main.calendar_query_intent("明日的行程") == "tomorrow"
    assert main.calendar_query_intent("後天行程") == "day_after_tomorrow"
    assert main.calendar_query_intent("這週的行程") == "week"
    assert main.calendar_query_intent("本週行程") == "week"
    assert main.calendar_query_intent("行程") == "today_tomorrow"
    assert main.calendar_query_intent("下週行程") is None


def test_single_day_and_week_calendar_cards(monkeypatch):
    calls = []
    event = {
        "summary": "拍攝",
        "start": {"dateTime": "2026-08-10T09:00:00+08:00"},
        "end": {"dateTime": "2026-08-10T11:00:00+08:00"},
    }

    def fake_fetch(start, end):
        calls.append((start, end))
        return [event]

    monkeypatch.setattr(main, "fetch_calendar_events", fake_fetch)
    now = main.datetime(2026, 8, 9, 18, 0, tzinfo=main.TAIPEI_TZ)

    tomorrow = main.calendar_command_message(now, "明天的行程")
    assert tomorrow["altText"] == "明日行程"
    assert tomorrow["contents"]["type"] == "bubble"
    assert len(calls) == 1
    assert calls[0][0].date().isoformat() == "2026-08-10"

    calls.clear()
    week = main.calendar_command_message(now, "這週的行程")
    assert week["altText"] == "本週行程"
    assert calls[0][0].date().isoformat() == "2026-08-03"
    assert calls[0][1].date().isoformat() == "2026-08-10"
    details = week["contents"]["body"]["contents"][0]["contents"][1]["text"]
    assert details == "08/10 09:00–11:00"
from starlette.requests import Request


QUOTE_USER_ID = "Ub983deb79584603885e5b28e9fdf2d5d"


def test_external_case_approval_owner_is_kao_er_hsien():
    assert main.EXTERNAL_CASE_OWNER_USER_ID == QUOTE_USER_ID
    assert main.EXTERNAL_CASE_TEST_MODE is False
    assert {
        "U6c6441cb38102499d1f80d4ea79a53ab",  # 周暐
        "Ub983deb79584603885e5b28e9fdf2d5d",  # 高爾賢
        "U9478b00702c716685d9d8b021d62d538",  # 阿筌
    }.issubset(main.INTERNAL_USER_IDS)


def test_external_case_owner_card_has_confirm_and_discuss_only():
    data = external_case.parse_initial("8月10號外案8萬", QUOTE_USER_ID, "爾賢")
    data.update({"projectName": "BWS", "caseType": "導演案", "destination": "公司", "paymentDate": "2026-09-15", "contact": "王小姐"})
    card = external_case.approval_card(data)
    assert card["type"] == "flex"
    actions = [button["action"] for button in card["contents"]["footer"]["contents"]]
    assert [(action["label"], action["text"].split(":")[0]) for action in actions] == [
        ("確認成立", "外案核准"), ("待討論", "外案待討論"),
    ]


def test_external_case_natural_message_and_tax_are_available_from_main_service():
    assert main.external_case is external_case
    data = external_case.parse_initial("8 月 10 號外案 3 萬", "U-test", "爾賢")
    assert data["amount"] == 30000
    assert external_case.tax_amounts(data) == (30000, 1500, 31500)
    assert external_case.next_step(data) == "details"


def test_external_case_ten_wan_never_reprompts_for_amount():
    for text in ["8 月 10 號外案 10 萬", "8月10號外案 100,000 元", "８月１０號外案１０萬", "8月10號外案十萬"]:
        data = external_case.parse_initial(text, "U-test", "爾賢")
        assert data["amount"] == 100000
        assert external_case.next_step(data) == "details"


def test_external_case_uses_two_step_prompt_and_one_final_confirmation():
    detail_prompt = external_case.prompt("details")
    assert detail_prompt["text"] == (
        "金額收到，請再補這五項\n\n"
        "案名：\n"
        "案型：導演案／剪接案／製片案／其他\n"
        "款項進入：公司／員工個人／尚未確認\n"
        "預計匯款日：\n"
        "聯繫窗口："
    )
    data = external_case.parse_initial("8月10號外案8萬", "U-test", "爾賢")
    data.update({
        "projectName": "測試專案", "caseType": "導演案", "destination": "公司",
        "paymentDate": "2026-09-15", "contact": "王小姐",
    })
    card = external_case.confirmation_card(data)
    assert card["type"] == "flex"
    action = card["contents"]["footer"]["contents"][0]["action"]
    assert action == {"type": "postback", "label": "確認送出", "data": "external:submit", "displayText": "確認送出"}
    modify = card["contents"]["footer"]["contents"][1]["action"]
    assert modify == {"type": "postback", "label": "修改", "data": "external:modify", "displayText": "修改外案資料"}


def test_external_case_five_field_reply_always_advances_to_confirmation():
    external_case.start("8月10號外案8萬", "U-five", "爾賢")
    session = external_case.accept_text(
        "U-five",
        "案名：品牌形象片\n案型：導演案\n款項進入：公司\n預計匯款日：9月15日\n聯繫窗口：王小姐",
    )
    assert session["step"] == "confirm"
    assert session["data"]["contact"] == "王小姐"


def test_external_case_modify_returns_to_details_and_preserves_values():
    user_id = "U9478b00702c716685d9d8b021d62d538"
    session = external_case.start("8月10號5萬塊的外案", user_id, "阿筌")
    session["data"].update({
        "projectName": "品牌片", "caseType": "導演案", "destination": "公司",
        "paymentDate": "2026-09-15", "contact": "王小姐",
    })
    session["step"] = "confirm"
    modified = external_case.begin_modify(user_id)
    assert modified["step"] == "details"
    assert modified["data"]["amount"] == 50000
    prompt = external_case.modification_prompt(modified["data"])["text"]
    assert "案名：品牌片" in prompt
    assert "聯繫窗口：王小姐" in prompt


@pytest.mark.parametrize("value", ["尚未確認", "不知道", "不確定", "未定", "還沒確定", "還不知道"])
def test_external_case_payment_date_unknown_words_are_canonical(value):
    assert external_case.parse_payment_date(f"預計匯款日：{value}") == "尚未確認"


def test_external_case_unknown_payment_date_can_reach_confirmation():
    user_id = "U9478b00702c716685d9d8b021d62d538"
    external_case.start("8月10號5萬塊的外案", user_id, "阿筌")
    session = external_case.accept_text(
        user_id,
        "案名：品牌片\n案型：導演案\n款項進入：公司\n預計匯款日：不知道\n聯繫窗口：王小姐",
    )
    assert session["step"] == "confirm"
    assert session["data"]["paymentDate"] == "尚未確認"


def test_external_case_tax_inclusive_amount_is_converted_to_pretax_for_bonus():
    data = external_case.parse_initial("8月10號10萬含稅的外案", "U-staff", "周暐")
    assert data["enteredAmount"] == 100000
    assert data["taxMode"] == "含稅"
    assert data["amount"] == 95238
    assert external_case.tax_amounts(data) == (95238, 4762, 100000)
    card = external_case.confirmation_card({
        **data, "projectName": "品牌片", "caseType": "導演案", "destination": "公司",
        "paymentDate": "尚未確認", "contact": "王小姐",
    })
    body_text = "\n".join(item["text"] for item in card["contents"]["body"]["contents"])
    assert "未稅金額：$95,238" in body_text
    assert "你的獎金 40%：$38,095" in body_text


def test_external_case_accepts_bare_numeric_amount_after_keyword():
    data = external_case.parse_initial("8月10號外案 100000", "U-staff", "阿筌")
    assert data["date"] == "2026-08-10"
    assert data["amount"] == 100000
    assert data["taxMode"] == "稅外"


def test_external_case_missing_amount_prompt_has_no_old_tax_copy():
    message = external_case.prompt("amount")
    assert message["text"] == "金額是多少？"


@pytest.mark.parametrize(("text", "expected"), [
    ("8月10號外案 500", 500),
    ("8月10號外案 8,500", 8500),
    ("8月10號外案 3千", 3000),
    ("8月10號外案 2.5k", 2500),
    ("8月10號外案 1.5萬", 15000),
    ("8月10號外案 一萬五", 15000),
    ("8月10號外案 十萬", 100000),
    ("8月10號外案 100000元", 100000),
])
def test_external_case_recognizes_common_amount_formats(text, expected):
    assert external_case.parse_amount(text) == expected


def test_external_case_amount_received_reply_matches_confirmed_copy():
    assert external_case.prompt("details")["text"] == (
        "金額收到，請再補這五項\n\n"
        "案名：\n"
        "案型：導演案／剪接案／製片案／其他\n"
        "款項進入：公司／員工個人／尚未確認\n"
        "預計匯款日：\n"
        "聯繫窗口："
    )


@pytest.mark.parametrize("text", [
    "8月10號5萬塊的外案",
    "8 月 10 號 5 萬塊的外案",
    "８月１０號５萬塊的外案",
    "8\u200b月\u200b10\u200b號 5萬塊的外案",
    "八月十號五萬塊的外案",
])
def test_achuan_external_case_date_and_amount_variants_go_to_details(text):
    user_id = "U9478b00702c716685d9d8b021d62d538"
    session = external_case.start(text, user_id, "阿筌")
    assert session["data"]["date"] == "2026-08-10"
    assert session["data"]["amount"] == 50000
    assert session["step"] == "details"
    assert external_case.prompt(session["step"])["text"].startswith("金額收到，請再補這五項")


def test_external_case_full_webhook_returns_visible_flex_confirmation(monkeypatch):
    user_id = main.EXTERNAL_CASE_OWNER_USER_ID
    replies = []
    monkeypatch.setattr(main, "verify_signature", lambda raw, signature: True)
    monkeypatch.setattr(main, "reply_messages", lambda token, messages: replies.append((token, messages)))
    external_case.SESSIONS.pop(user_id, None)

    def deliver(text, token):
        payload = {"events": [{
            "type": "message", "replyToken": token,
            "source": {"type": "user", "userId": user_id},
            "message": {"type": "text", "text": text},
        }]}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request({
            "type": "http", "method": "POST", "path": "/webhook",
            "headers": [(b"x-line-signature", b"test")],
        }, receive)
        assert asyncio.run(main.webhook(request)) == {"status": "ok"}

    deliver("8月10號外案8萬", "reply-start")
    deliver(
        "案名：品牌形象片\n案型：導演案\n款項進入：公司\n預計匯款日：9月15日\n聯繫窗口：王小姐",
        "reply-confirm",
    )
    assert replies[-1][0] == "reply-confirm"
    card = replies[-1][1][0]
    assert card["type"] == "flex"
    assert card["contents"]["footer"]["contents"][0]["action"]["data"] == "external:submit"


def test_external_case_submit_is_reported_saved_even_if_owner_push_fails(monkeypatch):
    user_id = "U6c6441cb38102499d1f80d4ea79a53ab"
    data = external_case.parse_initial("8月10號外案8萬", user_id, "周暐")
    data.update({"projectName": "BWS", "caseType": "導演案", "destination": "公司", "paymentDate": "2026-09-15", "contact": "王小姐"})
    external_case.SESSIONS[user_id] = {"data": data, "step": "confirm", "updatedAt": main.time.time()}
    replies = []
    monkeypatch.setattr(main, "verify_signature", lambda raw, signature: True)
    monkeypatch.setattr(external_case, "api_call", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(main, "push_messages", lambda *args, **kwargs: (_ for _ in ()).throw(main.requests.RequestException("push failed")))
    monkeypatch.setattr(main, "reply_text", lambda token, text: replies.append(text))
    payload = {"events": [{
        "type": "postback", "replyToken": "reply-submit", "source": {"type": "user", "userId": user_id},
        "postback": {"data": "external:submit"},
    }]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/webhook", "headers": [(b"x-line-signature", b"test")]}, receive)
    assert asyncio.run(main.webhook(request)) == {"status": "ok"}
    assert "資料已安全保存" in replies[-1]
    assert external_case.get_session(user_id) is None


def test_owner_self_submission_replies_with_pending_status_and_approval_card(monkeypatch):
    user_id = main.EXTERNAL_CASE_OWNER_USER_ID
    data = external_case.parse_initial("8月10號外案8萬", user_id, "爾賢")
    data.update({"projectName": "BWS", "caseType": "導演案", "destination": "公司", "paymentDate": "2026-09-15", "contact": "王小姐"})
    external_case.SESSIONS[user_id] = {"data": data, "step": "confirm", "updatedAt": main.time.time()}
    replies = []
    monkeypatch.setattr(main, "verify_signature", lambda raw, signature: True)
    monkeypatch.setattr(external_case, "api_call", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(main, "reply_messages", lambda token, messages: replies.append(messages))
    payload = {"events": [{"type": "postback", "replyToken": "reply-owner", "source": {"type": "user", "userId": user_id}, "postback": {"data": "external:submit"}}]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/webhook", "headers": [(b"x-line-signature", b"test")]}, receive)
    assert asyncio.run(main.webhook(request)) == {"status": "ok"}
    assert replies[-1][0]["text"].startswith("已送出，現在是「待核准」")
    assert [button["action"]["label"] for button in replies[-1][1]["contents"]["footer"]["contents"]] == ["確認成立", "待討論"]


def test_external_approval_card_uses_message_actions():
    data = external_case.parse_initial("8月10號外案8萬", "U-staff", "周暐")
    data.update({"projectName": "羽球", "caseType": "導演案", "destination": "公司", "paymentDate": "2026-09-15", "contact": "王小姐"})
    actions = [button["action"] for button in external_case.approval_card(data)["contents"]["footer"]["contents"]]
    assert actions == [
        {"type": "message", "label": "確認成立", "text": f"外案核准:{data['requestId']}"},
        {"type": "message", "label": "待討論", "text": f"外案待討論:{data['requestId']}"},
    ]


def test_owner_can_approve_external_case_via_message_action(monkeypatch):
    replies = []
    monkeypatch.setattr(main, "verify_signature", lambda raw, signature: True)
    monkeypatch.setattr(external_case, "api_call", lambda *args, **kwargs: {
        "ok": True, "employeeUserId": "U-staff", "projectName": "羽球",
    })
    monkeypatch.setattr(main, "reply_text", lambda token, text: replies.append(text))
    monkeypatch.setattr(main, "push_messages", lambda *args, **kwargs: None)
    request_id = "85edf6ec-a604-4ffe-8f40-73e8ee357db8"
    payload = {"events": [{
        "type": "message", "replyToken": "reply-approve",
        "source": {"type": "user", "userId": main.EXTERNAL_CASE_OWNER_USER_ID},
        "message": {"type": "text", "text": f"外案核准:{request_id}"},
    }]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/webhook", "headers": [(b"x-line-signature", b"test")]}, receive)
    assert asyncio.run(main.webhook(request)) == {"status": "ok"}
    assert replies == ["已核准，並完成登記。"]


def all_actions(value):
    """遞迴收集 Flex 圖卡操作，避免測試綁死視覺排版位置。"""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("action"), dict):
            found.append(value["action"])
        for child in value.values():
            found.extend(all_actions(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(all_actions(child))
    return found


def quote_event(event_type="postback", user_id=QUOTE_USER_ID, source_type="user"):
    event = {"type": event_type, "source": {"type": source_type, "userId": user_id}}
    if event_type == "postback":
        event["postback"] = {"data": "action=scheme&invitation=inv-123&scheme=A"}
    else:
        event["message"] = {"type": "text", "text": "確認"}
    return event


def test_quote_events_remain_limited_to_owner_and_explicit_actions():
    event = quote_event()
    assert main.is_quote_event(event) is True
    event["postback"]["data"] = "action=scheme&invitation=inv-123&scheme=D"
    assert main.is_quote_event(event) is False
    assert main.is_quote_event(quote_event(user_id="U-other")) is False
    text_event = quote_event(event_type="message")
    for text in ["確認", "送出", "主旨：Re: 測試報價"]:
        text_event["message"]["text"] = text
        assert main.is_quote_event(text_event) is True
    text_event["message"]["text"] = "代墊"
    assert main.is_quote_event(text_event) is False


def test_quote_forward_preserves_body_and_signature(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(main.requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    body = b'{"events":[]}'
    assert main.forward_quote_webhook(body, "original-signature") is True
    assert calls[0][1]["data"] is body
    assert calls[0][1]["headers"]["X-Line-Signature"] == "original-signature"


def test_mixed_quote_batch_keeps_existing_event(monkeypatch):
    payload = {
        "events": [
            quote_event(event_type="message"),
            {
                "type": "message",
                "replyToken": "reply-my-id",
                "source": {"type": "user", "userId": "U-other"},
                "message": {"type": "text", "text": "My ID"},
            },
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    forwarded, replies = [], []
    monkeypatch.setattr(main, "verify_signature", lambda raw, signature: True)
    monkeypatch.setattr(main, "forward_quote_webhook", lambda raw, signature: forwarded.append((raw, signature)) or True)
    monkeypatch.setattr(main, "reply_text", lambda token, text: replies.append((token, text)))
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/webhook",
        "headers": [(b"x-line-signature", b"original-signature")],
    }, receive)
    assert asyncio.run(main.webhook(request)) == {"status": "ok"}
    assert forwarded == [(body, "original-signature")]
    assert replies == [("reply-my-id", "User ID：U-other")]


def test_complete_expense_text_replies_with_confirmation_card(monkeypatch):
    """完整文字代墊必須直接回複確認圖卡，不能靜默或改走圖片流程。"""
    user_id = "U6c6441cb38102499d1f80d4ea79a53ab"
    payload = {"events": [{
        "type": "message", "replyToken": "reply-expense-text",
        "source": {"type": "user", "userId": user_id},
        "message": {"type": "text", "text": "代墊 PJR 專案加油汽油費 500 元"},
    }]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    replies = []
    monkeypatch.setattr(main, "verify_signature", lambda raw, signature: True)
    monkeypatch.setattr(main, "reply_messages", lambda token, messages: replies.append((token, messages)))
    main.EXPENSE_SESSIONS.pop(user_id, None)
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/webhook",
        "headers": [(b"x-line-signature", b"test")],
    }, receive)
    assert asyncio.run(main.webhook(request)) == {"status": "ok"}
    assert len(replies) == 1
    token, messages = replies[0]
    assert token == "reply-expense-text"
    assert messages[0]["altText"] == "確認代墊資料"
    payload = json.dumps(messages[0], ensure_ascii=False)
    assert "PJR" in payload
    assert "加油／汽油費" in payload


def test_text_receipt_link_keeps_text_fields_and_locks_first_image(monkeypatch):
    """補傳照片只能綁定附件，不能覆蓋文字判定的專案、項目與金額。"""
    monkeypatch.setattr(main.time, "time", lambda: 1000)
    session = {
        "step": "quick_confirm",
        "updated_at": 1000,
        "data": {"project": "PJR", "item": "交通", "amount": 500, "note": "加油費"},
    }
    main.activate_text_receipt_link("U-test", session)
    assert main.attach_receipt_to_text_session(session, "first-image", "image/jpeg", now=1001) == "attached"
    assert session["data"]["project"] == "PJR"
    assert session["data"]["item"] == "交通"
    assert session["data"]["amount"] == 500
    assert session["data"]["receiptBase64"] == "first-image"
    assert main.attach_receipt_to_text_session(session, "first-image", "image/jpeg", now=1002) == "duplicate"
    assert main.attach_receipt_to_text_session(session, "other-image", "image/jpeg", now=1002) == "locked"
    assert session["data"]["receiptBase64"] == "first-image"


def test_text_receipt_link_expires_after_five_minutes():
    session = {
        "sourceMode": "text",
        "linkExpiresAt": 1000,
        "data": {"project": "PJR", "item": "交通", "amount": 500},
    }
    assert main.attach_receipt_to_text_session(session, "late-image", "image/jpeg", now=1001) == "not_applicable"
    assert "receiptBase64" not in session["data"]


def test_new_session_starts_with_date():
    session = main.new_expense_session("U-test")
    assert session["step"] == "date"
    assert session["data"]["registrantUserId"] == "U-test"


def test_expired_session_is_removed(monkeypatch):
    session = main.new_expense_session("U-expired")
    session["updated_at"] = 0
    monkeypatch.setattr(main.time, "time", lambda: main.SESSION_TTL_SECONDS + 1)
    assert main.get_expense_session("U-expired") is None


def test_confirmation_switches_receipt_label():
    data = {
        "date": "2026-08-08",
        "project": "LINE Bot 測試",
        "item": "測試支出",
        "amount": 100,
        "category": "公司雜費",
        "payer": "周暐",
        "payment": "立即支付",
        "reimbursed": "否",
        "invoice": "未開",
        "note": "測試",
        "receiptBase64": "abc",
        "companyTaxIdValid": True,
    }
    card = main.build_expense_confirmation(data)
    payload = json.dumps(card, ensure_ascii=False)
    assert "已儲存" in payload
    assert "LINE Bot 測試" in payload


def test_parse_complete_natural_language(monkeypatch):
    class FixedDateTime(main.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 8, 10, 0, tzinfo=tz)

    monkeypatch.setattr(main, "datetime", FixedDateTime)
    data, missing = main.parse_expense_text(
        "代墊昨天 TOYOTA 拍攝餐費 850 元，我先付的，沒收據",
        "U6c6441cb38102499d1f80d4ea79a53ab",
    )
    assert missing == []
    assert data["date"] == "2026-08-07"
    assert data["project"] == "TOYOTA"
    assert data["item"] == "餐飲"
    assert data["amount"] == 850
    assert data["payer"] == "周暐"
    assert data["category"] == "案件支出（餐飲、道具、人員...）"
    assert "消費內容：拍攝餐費" in data["note"]
    assert "未附收據" in data["note"]


def test_parse_lists_all_missing_fields_at_once():
    data, missing = main.parse_expense_text(
        "代墊買東西",
        "U9478b00702c716685d9d8b021d62d538",
    )
    assert data["payer"] == "阿筌"
    assert "金額" in missing
    assert "專案名稱" in missing
    assert "項目分類或更清楚的消費內容" not in missing


def test_fuel_text_keeps_category_and_human_readable_content():
    data, missing = main.parse_expense_text(
        "代墊 PJR 專案加油汽油費 500 元",
        "U6c6441cb38102499d1f80d4ea79a53ab",
    )
    assert missing == []
    assert data["item"] == "交通"
    assert data["project"] == "PJR"
    assert data["expenseContent"] == "加油／汽油費"
    assert "消費內容：加油／汽油費" in data["note"]


def test_year_is_not_mistaken_for_amount():
    data, missing = main.parse_expense_text(
        "代墊 TOYOTA 2026 拍攝餐費",
        "U6c6441cb38102499d1f80d4ea79a53ab",
    )
    assert data["amount"] is None
    assert "金額" in missing


def test_explicit_amount_change_is_preferred():
    assert main.infer_amount("原本 850 元，金額改成 950") == 950


def test_multiple_currency_amounts_are_rejected():
    assert main.infer_amount("餐費 850 元，停車 120 元") is None


def test_clear_expense_intent_does_not_require_keyword():
    assert main.looks_like_expense_intent("我今天幫公司買硬碟 3280 元") is True
    assert main.looks_like_expense_intent("公司明天要買硬碟") is False


def test_expense_query_is_not_registration_intent():
    for text in ["我想詢問自己的代墊情況", "查詢我的代墊", "我的代墊統計", "代墊進度"]:
        assert main.looks_like_expense_query(text) is True
    assert main.looks_like_expense_query("代墊 PJR 餐費 500") is False


def test_expense_stats_card_shows_own_summary():
    card = main.expense_stats_card({"period": "2026-08", "count": 3, "total": 1500, "pendingCount": 2, "pendingTotal": 1000, "paidCount": 1, "paidTotal": 500})
    payload = json.dumps(card, ensure_ascii=False)
    assert "3 筆" in payload
    assert "2 筆／$1000" in payload


def test_receipt_analysis_creates_one_expense_row():
    analysis = {
        "isReceipt": True,
        "documentType": "電子發票",
        "merchantName": "測試餐廳",
        "date": "2026-08-08",
        "items": ["便當", "飲料"],
        "totalAmount": 350,
        "invoiceNumber": "AB12345678",
        "buyerTaxId": "9053-1465",
        "sellerTaxId": "12345678",
        "confidence": 0.96,
        "warnings": [],
    }
    data, missing = main.receipt_analysis_to_expense(
        analysis,
        "U6c6441cb38102499d1f80d4ea79a53ab",
        "base64-image",
        "image/jpeg",
    )
    assert data["item"] == "餐飲"
    assert "單據內容：便當、飲料" in data["note"]
    assert data["amount"] == 350
    assert data["category"] == "案件支出（餐飲、道具、人員...）"
    assert data["invoice"] == "是"
    assert data["companyTaxIdValid"] is True
    assert "統編" not in data["note"]
    assert missing == ["專案名稱"]


def test_receipt_rejects_clear_non_receipt_image():
    try:
        main.receipt_analysis_to_expense(
            {"isReceipt": False, "imageType": "人物風景照片"},
            "U6c6441cb38102499d1f80d4ea79a53ab",
            "image",
            "image/jpeg",
        )
    except ValueError as error:
        assert "不是可辨識的發票或收據" in str(error)
    else:
        raise AssertionError("非單據圖片必須被拒絕")


def test_receipt_does_not_guess_missing_total():
    analysis = {
        "isReceipt": True,
        "documentType": "收據",
        "merchantName": "測試商店",
        "date": "2026-08-08",
        "items": ["硬碟"],
        "totalAmount": None,
        "hasBusinessTaxId": False,
        "confidence": 0.5,
        "warnings": ["金額模糊"],
    }
    data, missing = main.receipt_analysis_to_expense(
        analysis,
        "U9478b00702c716685d9d8b021d62d538",
        "image",
        "image/jpeg",
    )
    assert data["amount"] is None
    assert "金額" in missing
    assert "影像辨識信心較低" in data["note"]


def test_receipt_false_flag_is_accepted_when_fields_are_valid():
    analysis = {
        "isReceipt": False,
        "imageType": "文件",
        "merchantName": "測試商店",
        "date": "2026-08-08",
        "items": ["耗材"],
        "totalAmount": 420,
        "confidence": 0.6,
    }
    data, missing = main.receipt_analysis_to_expense(
        analysis, "U9478b00702c716685d9d8b021d62d538", "image", "image/jpeg"
    )
    assert data["amount"] == 420
    assert data["item"] == ""
    assert "消費項目" in missing
    assert "專案名稱" in missing


def test_process_receipt_uses_second_pass_to_find_total(monkeypatch):
    results = iter([
        {"isReceipt": True, "merchantName": "測試店", "items": ["餐費"], "totalAmount": None},
        {"isReceipt": True, "date": "2026-08-08", "totalAmount": 880, "buyerTaxId": "90531465", "confidence": 0.9},
    ])

    def fake_analyze(image_base64, mime_type, focused_retry=False):
        return next(results)

    monkeypatch.setattr(main, "analyze_receipt_image", fake_analyze)
    data, _ = main.process_receipt_image(
        "U6c6441cb38102499d1f80d4ea79a53ab", "image", "image/jpeg"
    )
    assert data["amount"] == 880
    assert data["receiptSecondPass"] is True


def test_receipt_vision_reads_json_from_later_response_part(monkeypatch):
    """Gemini 將 JSON 放在後續 part 時，仍必須完成單據辨識。"""
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [{"content": {"parts": [
                    {"thought": True},
                    {"text": '{"isReceipt":true,"totalAmount":500,"items":["加油"]}'},
                ]}}]
            }

    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main.requests, "post", lambda *args, **kwargs: Response())
    result = main.analyze_receipt_image("image", "image/jpeg")
    assert result["isReceipt"] is True
    assert result["totalAmount"] == 500


def test_open_projects_filter_rank_without_age_or_limit():
    projects = [
        {"id": "old", "name": "舊專案", "status": "進行中", "updatedAt": "2026-01-01"},
        {"id": "closed", "name": "PJR 已結案", "status": "已結案", "updatedAt": "2026-08-01"},
        {"id": "pjr", "name": "PJR 廣告", "status": "進行中", "updatedAt": "2026-07-01", "aliases": ["吊車"]},
        *[
            {"id": str(index), "name": f"近期專案 {index}", "status": "執行中", "updatedAt": "2026-08-07"}
            for index in range(12)
        ],
    ]
    result = main.filter_recent_open_projects(projects, "PJR 吊車", main.datetime(2026, 8, 8))
    assert len(result) == 14
    assert result[0]["id"] == "pjr"
    assert all(project["id"] != "closed" for project in result)
    assert any(project["id"] == "old" for project in result)


def test_project_card_uses_short_index_postbacks():
    card = main.project_candidate_card([{"id": "p1", "name": "很長的專案名稱測試"}])
    actions = all_actions(card)
    assert actions[0]["data"] == "expense:project:0"
    assert actions[-2]["data"] == "expense:project:manual"
    assert actions[-1]["data"] == "expense:cancel"
    assert all(action["data"] != "expense:project:none" for action in actions)


def test_project_phrases_are_understood():
    assert main.infer_project_and_item("代墊 PJR 專案", None)[0] == "PJR"
    assert main.infer_project_and_item("這個專案是 MG50", None)[0] == "MG50"


def test_expense_supplements_accumulate_without_losing_receipt():
    existing = {
        "registrantUserId": "U-test",
        "date": "2026-07-06",
        "month": "7",
        "project": "",
        "item": "",
        "amount": None,
        "category": "",
        "payer": "周暐",
        "payment": "已支出",
        "reimbursed": "否",
        "invoice": "未開",
        "receiptBase64": "receipt-image",
        "receiptMimeType": "image/jpeg",
        "companyTaxIdValid": True,
    }
    after_project = main.merge_expense_text(existing, "這個專案是 MG50", "U-test")
    completed = main.merge_expense_text(after_project, "加油 $500", "U-test")
    assert completed["project"] == "MG50"
    assert completed["item"] == "交通"
    assert completed["amount"] == 500
    assert completed["category"] == main.PROJECT_EXPENSE_CATEGORY
    assert completed["receiptBase64"] == "receipt-image"
    assert main.missing_expense_fields(completed) == []


def test_cpc_receipt_is_classified_as_transportation():
    analysis = {
        "isReceipt": True,
        "documentType": "電子發票",
        "merchantName": "台灣中油",
        "date": "2026-07-06",
        "items": ["九五無鉛汽油"],
        "totalAmount": 500,
        "buyerTaxId": "90531465",
        "confidence": 0.95,
    }
    data, missing = main.receipt_analysis_to_expense(
        analysis, "U6c6441cb38102499d1f80d4ea79a53ab", "image", "image/jpeg"
    )
    assert data["item"] == "交通"
    assert data["amount"] == 500
    assert missing == ["專案名稱"]


def test_unknown_item_and_no_answer_show_action_cards(monkeypatch):
    session = {"raw_text": "", "data": {"project": "PJR", "item": "", "amount": 500, "category": "", "payer": "周暐"}}
    message = main.build_project_or_missing_prompt(session, ["消費項目", "項目分類或更清楚的消費內容"])
    buttons = message["contents"]["body"]["contents"]
    assert buttons[0]["action"]["data"] == "expense:item:交通"
    assert buttons[-1]["action"]["data"] == "expense:cancel"

    monkeypatch.setattr(main, "get_recent_open_projects", lambda context: [])
    project_message = main.build_project_or_missing_prompt(session, ["專案名稱"])
    project_actions = all_actions(project_message)
    assert project_actions[0]["data"] == "expense:project:search"
    assert project_actions[-1]["data"] == "expense:cancel"


def test_submit_expense_adds_attachment_metadata(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    captured = []

    def fake_post(url, params, json, timeout):
        captured.append(json)
        if json["action"] == "expense_receipt_upload":
            return FakeResponse({"ok": True, "receiptUrl": "https://drive.google.com/test"})
        return FakeResponse({"ok": True, "row": 12, "transactionId": json["expense"]["transactionId"], "receiptUrl": json["expense"]["receiptUrl"]})

    monkeypatch.setattr(main, "EXPENSE_API_URL", "https://example.com/expense")
    monkeypatch.setattr(main, "EXPENSE_API_KEY", "secret")
    monkeypatch.setattr(main.requests, "post", fake_post)
    main.submit_expense({
        "date": "2026-08-08", "project": "PJR", "payer": "周暐", "amount": 500,
        "receiptBase64": "image", "receiptMimeType": "image/jpeg", "companyTaxIdValid": True,
    })
    upload = captured[0]["expense"]
    sheet_write = captured[1]["expense"]
    assert upload["transactionId"]
    assert upload["receiptFileName"].endswith(".jpg")
    assert sheet_write["receiptUrl"] == "https://drive.google.com/test"
    assert "receiptBase64" not in sheet_write


def test_submit_expense_retries_once_when_apps_script_returns_html(monkeypatch):
    """Apps Script 暫時回傳非 JSON 時，沿用同一交易編號重試且不得建立重複資料。"""
    calls = []

    class FakeResponse:
        def __init__(self, valid):
            self.valid = valid

        def raise_for_status(self):
            return None

        def json(self):
            if not self.valid:
                raise ValueError("Expecting value: line 1 column 1")
            return {"ok": True, "row": 12, "transactionId": "tx-1"}

    def fake_post(url, params, json, timeout):
        calls.append(json["expense"]["transactionId"])
        return FakeResponse(valid=len(calls) == 2)

    monkeypatch.setattr(main, "EXPENSE_API_URL", "https://example.com/expense")
    monkeypatch.setattr(main, "EXPENSE_API_KEY", "secret")
    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    result = main.submit_expense({"date": "2026-08-08", "project": "PJR", "amount": 500})
    assert result["ok"] is True
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_submit_expense_recovers_by_transaction_id_after_unknown_response(monkeypatch):
    """兩次寫入回應皆異常時，以交易編號查到資料就視為成功。"""
    actions = []

    class FakeResponse:
        def __init__(self, action):
            self.action = action

        def raise_for_status(self):
            return None

        def json(self):
            if self.action == "expense":
                raise ValueError("HTML response")
            return {"ok": True, "found": True, "row": 22, "transactionId": "tx-recovered", "recordUrl": "https://sheet.example/22"}

    def fake_post(url, params, json, timeout):
        actions.append(json["action"])
        return FakeResponse(json["action"])

    monkeypatch.setattr(main, "EXPENSE_API_URL", "https://example.com/expense")
    monkeypatch.setattr(main, "EXPENSE_API_KEY", "secret")
    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)
    result = main.submit_expense({"transactionId": "tx-recovered", "project": "PJR", "amount": 500})
    assert result["row"] == 22
    assert actions == ["expense", "expense", "expense_status"]


def test_crane_expense_is_classified_as_project_expense():
    assert main.infer_category("PJR 專案吊車費用") == "案件支出（餐飲、道具、人員...）"
    assert main.infer_category("拍攝現場機具租賃") == "案件支出（餐飲、道具、人員...）"


def test_seller_tax_id_cannot_pass_company_validation():
    analysis = {"buyerTaxId": "12345678", "sellerTaxId": "90531465"}
    assert main.has_valid_company_tax_id(analysis) is False
    analysis["buyerTaxId"] = "9053-1465"
    assert main.has_valid_company_tax_id(analysis) is True


def test_company_tax_id_can_be_recovered_from_ocr_raw_text():
    """Gemini 未標成買方時，OCR 原文的完整公司統編仍須辨識成功。"""
    analysis = {"buyerTaxId": "", "sellerTaxId": "12345678", "rawText": "買受人 9053-1465\n總計 500"}
    assert main.has_valid_company_tax_id(analysis) is True
    analysis["rawText"] = "買受人 9053-146\n總計 500"
    assert main.has_valid_company_tax_id(analysis) is False


def test_pending_text_merges_into_processing_receipt_without_losing_image():
    """OCR 期間收到的文字必須與收據合成同一筆。"""
    user_id = "U6c6441cb38102499d1f80d4ea79a53ab"
    main.EXPENSE_SESSIONS[user_id] = {
        "step": "receipt_processing", "updated_at": main.time.time(),
        "pending_text": "代墊 PJR 專案加油 500 元", "data": {"registrantUserId": user_id},
    }
    receipt = {
        "registrantUserId": user_id, "date": "2026-08-09", "item": "交通", "amount": 500,
        "payer": "周暐", "receiptBase64": "receipt-image", "receiptMimeType": "image/jpeg",
    }
    merged = main.merge_pending_receipt_text(user_id, receipt)
    assert merged["project"] == "PJR"
    assert merged["receiptBase64"] == "receipt-image"
    assert merged["expenseContent"] == "加油／汽油費"


def test_invalid_company_tax_id_warns_without_blocking_or_showing_number():
    data = {"receiptBase64": "image", "companyTaxIdValid": False, "buyerTaxId": "12345678"}
    card = main.build_expense_confirmation(data)
    assert card["altText"] == "確認代墊資料"
    assert "12345678" not in json.dumps(card, ensure_ascii=False)
    assert "此單據未辨識到公司統編" in json.dumps(card, ensure_ascii=False)
    assert "expense:confirm" in json.dumps(card, ensure_ascii=False)


def test_batch_expires_after_fifteen_minutes(monkeypatch):
    main.EXPENSE_BATCHES["U-batch"] = {"updated_at": 0, "project": "PJR"}
    monkeypatch.setattr(main.time, "time", lambda: main.BATCH_TTL_SECONDS + 1)
    assert main.get_expense_batch("U-batch") is None


def test_recent_project_is_kept_for_twenty_four_hours(monkeypatch):
    main.RECENT_EXPENSE_PROJECTS["U-recent"] = {"updated_at": 100, "project": "PJR"}
    monkeypatch.setattr(main.time, "time", lambda: 100 + main.RECENT_PROJECT_TTL_SECONDS - 1)
    assert main.get_recent_expense_project("U-recent") == "PJR"
    monkeypatch.setattr(main.time, "time", lambda: 100 + main.RECENT_PROJECT_TTL_SECONDS + 1)
    assert main.get_recent_expense_project("U-recent") == ""


def test_batch_summary_has_exactly_two_final_actions():
    card = main.expense_batch_summary_card({"count": 2, "total": 900, "notes": ["第 1 筆未填寫公司統編"], "recordUrls": ["https://example.com/row"]})
    actions = all_actions(card)
    assert len(actions) == 2
    assert actions[0]["label"] == "新的專案登記代墊"
    assert actions[0]["data"] == "expense:start_new"
    assert actions[1]["label"] == "完成結束"
    assert actions[1]["data"] == "expense:finish_summary"


def test_project_card_paginates_with_absolute_indexes():
    projects = [{"id": str(i), "name": f"專案 {i}"} for i in range(15)]
    first = main.project_candidate_card(projects)
    assert all_actions(first)[7]["data"] == "expense:project_page:1"
    second = main.project_candidate_card(projects, 1)
    assert all_actions(second)[0]["data"] == "expense:project:7"


def test_result_cards_distinguish_success_and_duplicate():
    data = {"project": "PJR", "item": "交通", "amount": 500}
    success = main.expense_result_card(data, {"ok": True, "row": 12, "transactionId": "tx-1", "recordUrl": "https://example.com/row", "continuous": True})
    duplicate = main.expense_result_card(data, {"ok": True, "duplicate": True, "recordUrl": "https://example.com/old"})
    assert success["contents"]["header"]["contents"][-1]["text"] == "代墊已登記，待補收據"
    assert any(action.get("data") == "expense:new" for action in all_actions(success))
    assert duplicate["contents"]["header"]["contents"][-1]["text"] == "這張單據已登記過"
    assert all(action.get("data") != "expense:new" for action in all_actions(duplicate))


def test_supplement_list_and_detail_cards():
    items = [{"row": 12, "date": "2026-08-08", "project": "PJR", "amount": 500, "reasons": ["缺少統編", "圖片不清楚"]}]
    listing = main.supplement_list_card(items)
    assert any(action.get("data") == "supplement:select:12" for action in all_actions(listing))
    detail = main.supplement_detail_card(items[0])
    payload = json.dumps(detail, ensure_ascii=False)
    assert "supplement:accept_no_tax:12" in payload
    assert "supplement:retake:12" in payload


def test_empty_supplement_list_is_clear():
    assert main.supplement_list_card([])["text"] == "目前沒有待補件資料。"


def test_duplicate_card_shows_original_registrant():
    card = main.expense_result_card({}, {"duplicate": True, "original": {"date": "2026-08-08", "project": "PJR", "amount": 500, "registrantName": "高爾賢"}})
    assert "高爾賢" in json.dumps(card, ensure_ascii=False)


def test_reply_success_does_not_use_push(monkeypatch):
    """Reply 正常成功時不得額外消耗 Push 額度。"""
    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

    monkeypatch.setattr(main.requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or Response())
    main.DELIVERED_LINE_EVENTS.clear()
    main.CURRENT_LINE_USER_ID.set("U6c6441cb38102499d1f80d4ea79a53ab")
    main.CURRENT_LINE_SOURCE_TYPE.set("user")
    main.CURRENT_WEBHOOK_EVENT_ID.set("evt-reply-ok")

    messages = [{"type": "text", "text": "測試成功"}]
    main.reply_messages("valid-reply-token", messages)

    assert len(calls) == 1
    assert calls[0][0].endswith("/reply")
    assert calls[0][1]["json"]["messages"] == messages
    assert "evt-reply-ok" in main.DELIVERED_LINE_EVENTS


def test_expired_reply_token_falls_back_to_push(monkeypatch):
    """Reply Token 逾時後，內部員工仍須收到完全相同的 Push 訊息。"""
    calls = []

    class ReplyExpiredResponse:
        status_code = 400

        def raise_for_status(self):
            error = main.requests.HTTPError("invalid reply token")
            error.response = self
            raise error

    class PushSuccessResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return ReplyExpiredResponse() if url.endswith("/reply") else PushSuccessResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)
    main.DELIVERED_LINE_EVENTS.clear()
    user_id = "U6c6441cb38102499d1f80d4ea79a53ab"
    main.CURRENT_LINE_USER_ID.set(user_id)
    main.CURRENT_LINE_SOURCE_TYPE.set("user")
    main.CURRENT_WEBHOOK_EVENT_ID.set("evt-push-fallback")

    messages = [{"type": "text", "text": "備援成功"}]
    main.reply_messages("expired-reply-token", messages)

    assert [call[0].rsplit("/", 1)[-1] for call in calls] == ["reply", "push"]
    assert calls[1][1]["json"] == {"to": user_id, "messages": messages}
    assert "evt-push-fallback" in main.DELIVERED_LINE_EVENTS


def test_delivered_webhook_event_is_not_sent_twice(monkeypatch):
    """LINE 重送同一 webhookEventId 時，不得再次回覆或重做登記。"""
    monkeypatch.setattr(
        main.requests,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不應再次呼叫 LINE API")),
    )
    main.DELIVERED_LINE_EVENTS.clear()
    main.DELIVERED_LINE_EVENTS["evt-already-delivered"] = main.time.time()
    main.CURRENT_LINE_USER_ID.set("U6c6441cb38102499d1f80d4ea79a53ab")
    main.CURRENT_LINE_SOURCE_TYPE.set("user")
    main.CURRENT_WEBHOOK_EVENT_ID.set("evt-already-delivered")

    main.reply_text("replayed-token", "不應重送")
