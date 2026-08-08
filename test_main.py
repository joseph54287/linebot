"""代墊登記核心流程測試。"""

import asyncio
import json

import main
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
    assert data["note"] == "未附收據"


def test_parse_lists_all_missing_fields_at_once():
    data, missing = main.parse_expense_text(
        "代墊買東西",
        "U9478b00702c716685d9d8b021d62d538",
    )
    assert data["payer"] == "阿全"
    assert "金額" in missing
    assert "專案名稱（沒有專案請寫「專案無」）" in missing
    assert "項目分類或更清楚的消費內容" in missing


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


def test_receipt_analysis_creates_one_expense_row():
    analysis = {
        "isReceipt": True,
        "documentType": "電子發票",
        "merchantName": "測試餐廳",
        "date": "2026-08-08",
        "items": ["便當", "飲料"],
        "totalAmount": 350,
        "invoiceNumber": "AB12345678",
        "taxId": "12345678",
        "hasBusinessTaxId": True,
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
    assert missing == ["專案名稱（沒有專案請寫「專案無」）"]


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
    assert "專案名稱（沒有專案請寫「專案無」）" in missing


def test_process_receipt_uses_second_pass_to_find_total(monkeypatch):
    results = iter([
        {"isReceipt": True, "merchantName": "測試店", "items": ["餐費"], "totalAmount": None},
        {"isReceipt": True, "date": "2026-08-08", "totalAmount": 880, "confidence": 0.9},
    ])

    def fake_analyze(image_base64, mime_type, focused_retry=False):
        return next(results)

    monkeypatch.setattr(main, "analyze_receipt_image", fake_analyze)
    data, _ = main.process_receipt_image(
        "U6c6441cb38102499d1f80d4ea79a53ab", "image", "image/jpeg"
    )
    assert data["amount"] == 880
    assert data["receiptSecondPass"] is True


def test_recent_projects_filter_rank_and_limit():
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
    assert len(result) == 10
    assert result[0]["id"] == "pjr"
    assert all(project["id"] not in {"old", "closed"} for project in result)


def test_project_card_uses_short_index_postbacks():
    card = main.project_candidate_card([{"id": "p1", "name": "很長的專案名稱測試"}])
    buttons = card["contents"]["body"]["contents"]
    assert buttons[0]["action"]["data"] == "expense:project:0"
    assert buttons[-3]["action"]["data"] == "expense:project:manual"
    assert buttons[-2]["action"]["data"] == "expense:project:none"
    assert buttons[-1]["action"]["data"] == "expense:cancel"


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
        "confidence": 0.95,
    }
    data, missing = main.receipt_analysis_to_expense(
        analysis, "U6c6441cb38102499d1f80d4ea79a53ab", "image", "image/jpeg"
    )
    assert data["item"] == "交通"
    assert data["amount"] == 500
    assert missing == ["專案名稱（沒有專案請寫「專案無」）"]


def test_unknown_item_and_no_answer_show_action_cards(monkeypatch):
    session = {"raw_text": "", "data": {"project": "PJR", "item": "", "amount": 500, "category": "", "payer": "周暐"}}
    message = main.build_project_or_missing_prompt(session, ["消費項目", "項目分類或更清楚的消費內容"])
    buttons = message["contents"]["body"]["contents"]
    assert buttons[0]["action"]["data"] == "expense:item:交通"
    assert buttons[-1]["action"]["data"] == "expense:cancel"

    monkeypatch.setattr(main, "get_recent_open_projects", lambda context: [])
    project_message = main.build_project_or_missing_prompt(session, ["專案名稱（沒有專案請寫「專案無」）"])
    project_buttons = project_message["contents"]["body"]["contents"]
    assert project_buttons[0]["action"]["data"] == "expense:project:manual"
    assert project_buttons[-1]["action"]["data"] == "expense:cancel"


def test_submit_expense_adds_attachment_metadata(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "receiptUrl": "https://drive.google.com/test"}

    captured = {}

    def fake_post(url, params, json, timeout):
        captured.update(json["expense"])
        return FakeResponse()

    monkeypatch.setattr(main, "EXPENSE_API_URL", "https://example.com/expense")
    monkeypatch.setattr(main, "EXPENSE_API_KEY", "secret")
    monkeypatch.setattr(main.requests, "post", fake_post)
    main.submit_expense({
        "date": "2026-08-08", "project": "PJR", "payer": "周暐", "amount": 500,
        "receiptBase64": "image", "receiptMimeType": "image/jpeg",
    })
    assert captured["transactionId"]
    assert captured["receiptFileName"].endswith(".jpg")


def test_crane_expense_is_classified_as_project_expense():
    assert main.infer_category("PJR 專案吊車費用") == "案件支出（餐飲、道具、人員...）"
    assert main.infer_category("拍攝現場機具租賃") == "案件支出（餐飲、道具、人員...）"
