"""auth-pages 規格「隱私權政策如實描述資料權利」：政策頁描述已提供的功能，保存期限表與清除指令使用的數值一致。"""
import re
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from app import create_app
from app.config import load_config
from app.models import Base
from app.services import data_retention_service as retention
from app.services.legal import CURRENT_TERMS_VERSION
from tests.support.auth import FakeClock


@pytest.fixture()
def client():
    app = create_app(load_config(environ={"APP_ENV": "testing"}), clock=FakeClock())
    Base.metadata.create_all(app.extensions["db_engine"])
    return app.test_client()


class _TableRows(HTMLParser):
    """把頁面上的表格抽成（第一欄文字，第二欄文字）。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def retention_table(html: str) -> dict[str, str]:
    parser = _TableRows()
    parser.feed(html)
    return {row[0]: row[1] for row in parser.rows if len(row) == 2}


def period_text(period: timedelta) -> str:
    """與政策頁相同的寫法：365 天 = 12 個月、730 天 = 24 個月，其餘用天。"""
    months = {365: 12, 730: 24}
    return f"{months[period.days]} 個月" if period.days in months else f"{period.days} 天"


def test_period_text_uses_months_only_for_the_two_long_periods():
    assert period_text(timedelta(days=365)) == "12 個月" and period_text(timedelta(days=730)) == "24 個月"
    assert period_text(timedelta(days=14)) == "14 天" and period_text(timedelta(days=7)) == "7 天"


def test_the_retention_table_uses_the_same_periods_as_the_purge_command(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    expectations = {
        "對話與訊息": retention.CONVERSATION_RETENTION,
        "用量紀錄（不含對話全文）": retention.LEDGER_RETENTION,
        "模型回覆失敗時的原始輸出": retention.ERROR_DETAIL_RETENTION,
        "信箱驗證與重設密碼的一次性權杖": retention.AUTH_TOKEN_RETENTION,
        "管理稽核紀錄": retention.ADMIN_AUDIT_RETENTION,
    }
    for label, period in expectations.items():
        assert label in table, label
        assert period_text(period) in table[label], (label, table[label])
    # 每一列只含自己的期限，沒有把別類的數字誤植進來
    assert "12 個月" not in table["管理稽核紀錄"] and "24 個月" not in table["對話與訊息"]


def test_the_table_also_discloses_the_tombstone_that_is_kept_without_a_limit(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (label,) = [key for key in table if key.startswith("帳號刪除紀錄")]
    assert "不含個人資料" in label and "不設期限" in table[label]


def test_the_rights_section_describes_the_real_features_and_no_longer_says_they_are_coming(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "後續版本提供" not in html and "將提供" not in html and "將於" not in html
    section = html.split("<h2>5. 您的權利</h2>", 1)[1].split("<h2>6.", 1)[0]
    for needle in ("匯出我的資料", "下載 JSON", "CSV（zip）", "含生日與年齡區間", "刪除帳號", "7 天寬限期", "撤銷刪除", "永久刪除",
                   "匿名化保留統計", "備份最長 30 天", "管理員帳號無法自行刪除"):
        assert needle in section, needle


def test_the_privacy_page_numbers_for_age_still_come_from_the_policy_not_the_text(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert re.search(r"未滿 \d+ 歲者無法註冊", html)


def test_the_terms_version_names_the_new_draft_and_the_registration_records_it():
    assert CURRENT_TERMS_VERSION == "2026-10-30-draft-3" and len(CURRENT_TERMS_VERSION) <= 40
