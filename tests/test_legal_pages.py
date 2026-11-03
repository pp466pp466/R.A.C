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
    assert re.search(r"發現未滿 \d+ 歲者", html)


def test_the_terms_version_names_the_new_draft_and_the_registration_records_it():
    assert CURRENT_TERMS_VERSION == "2026-11-02-draft-4" and len(CURRENT_TERMS_VERSION) <= 40


# ---- Google 登入改版（google-first-login）新增的揭露 ------------------------------------------------------

def test_the_privacy_policy_says_the_account_is_created_at_the_first_google_sign_in_with_the_google_email_and_name(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "第一次以 Google 登入" in html and "建立帳號" in html and "Google 提供的電子郵件與名稱" in html
    assert "玩家帳號沒有密碼" in html and "只有管理員帳號有密碼" in html


def test_the_privacy_policy_says_the_age_is_declared_after_signing_in_and_unfinished_or_underage_accounts_are_deleted_at_once(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "登入之後" in html and "年齡確認" in html
    assert "24 小時內沒有完成年齡確認，帳號與資料會立即刪除" in html
    assert re.search(r"發現未滿 \d+ 歲者，帳號與全部資料也會(?:<strong>)?立即刪除", html)
    table = retention_table(html)
    (account_row,) = [value for key, value in table.items() if key.startswith("帳號資料")]
    assert "24 小時內未完成年齡確認" in account_row and "立即刪除" in account_row


def test_the_retention_table_lists_the_lock_strikes_with_the_purge_period_and_the_appeals_with_their_account(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (strike_label,) = [key for key in table if key.startswith("帳號鎖定計次")]
    assert period_text(retention.LOCK_STRIKE_RETENTION) in table[strike_label] and "只含帳號編號" in strike_label
    (appeal_label,) = [key for key in table if key.startswith("申訴")]
    assert "帳號刪除時一併刪除" in table[appeal_label] and "與所屬帳號相同" in table[appeal_label]


def test_the_privacy_policy_discloses_appeals_as_personal_data_only_the_admin_can_read_while_locked(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "申訴" in html and "只有管理員在您的帳號被鎖定期間看得到" in html and "不會出現在日誌" in html
    assert "鎖定的計次" in html                                                     # 使用紀錄裡也揭露了計次
    assert "以及您送出的申訴" in html                                               # 匯出的內容包含申訴


def test_the_terms_describe_the_google_first_flow_the_age_step_the_deadline_and_the_automatic_lock(client):
    html = client.get("/terms").get_data(as_text=True)
    assert "第一次以 Google 登入時會建立您的帳號" in html and "不另外設定密碼" in html
    assert "24 小時內沒有完成年齡確認，帳號與其資料會被刪除" in html and "立即刪除" in html
    assert "自動鎖定" in html and "只有管理員能解除" in html and "帳號申訴" in html
    assert "閒置 30 天" in html


def test_the_terms_no_longer_point_to_a_registration_page(client):
    html = client.get("/terms").get_data(as_text=True)
    assert 'href="/register"' not in html and "前往註冊" not in html
    assert 'href="/login"' in html and 'href="/privacy"' in html
    assert "請到「帳號設定」補填" not in html                                       # 舊的補填說法已不存在
