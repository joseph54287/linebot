"""代墊登記核心流程測試。"""

import main


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
    assert data["item"] == "拍攝餐費"
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
