"""代墊登記核心流程測試。"""

import asyncio
import json

import main


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
    event["message"]["text"] = "本週行程"
    assert main.is_calendar_command(event) is False
    event["message"]["text"] = "行程"
    event["source"]["userId"] = "U-other"
    assert main.is_calendar_command(event) is False
    event["source"] = {"type": "group", "userId": main.QUOTE_OWNER_USER_ID}
    assert main.is_calendar_command(event) is False
from starlette.requests import Request


QUOTE_USER_ID = "Ub983deb79584603885e5b28e9fdf2d5d"


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
    assert messages[0]["altText"] == "請確認代墊資料"
    summary = messages[0]["contents"]["body"]["contents"][0]["text"]
    assert "專案：PJR" in summary
    assert "內容：加油／汽油費" in summary


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
    summary = card["contents"]["body"]["contents"][0]["text"]
    assert "收據：已附照片" in summary
    assert "LINE Bot 測試" in summary


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
    assert data["payer"] == "阿全"
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
    text = card["contents"]["body"]["contents"][0]["text"]
    assert "登記：3 筆" in text
    assert "待撥款：2 筆／$1000" in text


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
    buttons = card["contents"]["body"]["contents"]
    assert buttons[0]["action"]["data"] == "expense:project:0"
    assert buttons[-2]["action"]["data"] == "expense:project:search"
    assert buttons[-1]["action"]["data"] == "expense:cancel"
    assert all(button["action"]["data"] != "expense:project:none" for button in buttons)


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
    project_buttons = project_message["contents"]["body"]["contents"]
    assert project_buttons[0]["action"]["data"] == "expense:project:search"
    assert project_buttons[-1]["action"]["data"] == "expense:cancel"


def test_submit_expense_adds_attachment_metadata(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "row": 12, "transactionId": "tx-1", "receiptUrl": "https://drive.google.com/test"}

    captured = {}

    def fake_post(url, params, json, timeout):
        captured.update(json["expense"])
        return FakeResponse()

    monkeypatch.setattr(main, "EXPENSE_API_URL", "https://example.com/expense")
    monkeypatch.setattr(main, "EXPENSE_API_KEY", "secret")
    monkeypatch.setattr(main.requests, "post", fake_post)
    main.submit_expense({
        "date": "2026-08-08", "project": "PJR", "payer": "周暐", "amount": 500,
        "receiptBase64": "image", "receiptMimeType": "image/jpeg", "companyTaxIdValid": True,
    })
    assert captured["transactionId"]
    assert captured["receiptFileName"].endswith(".jpg")


def test_crane_expense_is_classified_as_project_expense():
    assert main.infer_category("PJR 專案吊車費用") == "案件支出（餐飲、道具、人員...）"
    assert main.infer_category("拍攝現場機具租賃") == "案件支出（餐飲、道具、人員...）"


def test_seller_tax_id_cannot_pass_company_validation():
    analysis = {"buyerTaxId": "12345678", "sellerTaxId": "90531465"}
    assert main.has_valid_company_tax_id(analysis) is False
    analysis["buyerTaxId"] = "9053-1465"
    assert main.has_valid_company_tax_id(analysis) is True


def test_invalid_company_tax_id_warns_without_blocking_or_showing_number():
    data = {"receiptBase64": "image", "companyTaxIdValid": False, "buyerTaxId": "12345678"}
    card = main.build_expense_confirmation(data)
    assert card["altText"] == "請確認代墊資料"
    assert "12345678" not in json.dumps(card, ensure_ascii=False)
    assert "⚠ 此單據未填寫公司統編" in json.dumps(card, ensure_ascii=False)
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
    actions = card["template"]["actions"]
    assert len(actions) == 2
    assert actions[0]["label"] == "新的專案登記代墊"
    assert actions[0]["data"] == "expense:start_new"
    assert actions[1]["label"] == "完成結束"
    assert actions[1]["data"] == "expense:finish_summary"


def test_project_card_paginates_with_absolute_indexes():
    projects = [{"id": str(i), "name": f"專案 {i}"} for i in range(15)]
    first = main.project_candidate_card(projects)
    assert first["contents"]["body"]["contents"][7]["action"]["data"] == "expense:project_page:1"
    second = main.project_candidate_card(projects, 1)
    assert second["contents"]["body"]["contents"][0]["action"]["data"] == "expense:project:7"


def test_result_cards_distinguish_success_and_duplicate():
    data = {"project": "PJR", "item": "交通", "amount": 500}
    success = main.expense_result_card(data, {"ok": True, "recordUrl": "https://example.com/row", "continuous": True})
    duplicate = main.expense_result_card(data, {"ok": True, "duplicate": True, "recordUrl": "https://example.com/old"})
    assert success["template"]["title"] == "代墊登記完成"
    assert any(action.get("data") == "expense:new" for action in success["template"]["actions"])
    assert duplicate["template"]["title"] == "這張單據已登記過"
    assert all(action.get("data") != "expense:new" for action in duplicate["template"]["actions"])


def test_supplement_list_and_detail_cards():
    items = [{"row": 12, "date": "2026-08-08", "project": "PJR", "amount": 500, "reasons": ["缺少統編", "圖片不清楚"]}]
    listing = main.supplement_list_card(items)
    assert listing["contents"]["body"]["contents"][0]["action"]["data"] == "supplement:select:12"
    detail = main.supplement_detail_card(items[0])
    payload = json.dumps(detail, ensure_ascii=False)
    assert "supplement:accept_no_tax:12" in payload
    assert "supplement:retake:12" in payload


def test_empty_supplement_list_is_clear():
    assert main.supplement_list_card([])["text"] == "目前沒有待補件資料。"


def test_duplicate_card_shows_original_registrant():
    card = main.expense_result_card({}, {"duplicate": True, "original": {"date": "2026-08-08", "project": "PJR", "amount": 500, "registrantName": "高爾賢"}})
    assert "高爾賢" in card["template"]["text"]


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
