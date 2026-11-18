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
        "管理員重設密碼的一次性權杖": retention.AUTH_TOKEN_RETENTION,
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
    section = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    for needle in ("匯出我的資料", "下載 JSON", "CSV（zip）", "含生日與年齡區間", "刪除帳號", "7 天寬限期", "撤銷刪除", "永久刪除",
                   "匿名化保留統計", "備份最長 30 天", "管理員帳號無法自行刪除"):
        assert needle in section, needle


def test_the_privacy_page_numbers_for_age_still_come_from_the_policy_not_the_text(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert re.search(r"發現未滿 \d+ 歲者", html)


def test_the_terms_version_names_the_new_draft_and_the_registration_records_it():
    # rpg-turn-revision 補了重新生成理由與遊戲幣帳本的告知（draft-6），之後又把編輯改為與重新生成共用免費次數（draft-7）；
    # adult-only-signup 加入僅限滿 18 歲、註冊時的年滿 18 歲聲明與謊報責任（draft-8，2026-11-18）；
    # 版本隨內容提高（只影響之後新註冊者記錄的版本）
    assert CURRENT_TERMS_VERSION == "2026-11-18-draft-8" and len(CURRENT_TERMS_VERSION) <= 40
    assert CURRENT_TERMS_VERSION != "2026-11-16-draft-7"            # 提高過：不是先前的版本


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


def test_the_privacy_policy_no_longer_points_to_a_registration_page_either(client):
    """沒有 /register：隱私權政策與條款一樣，底部連結是登入頁（登入與註冊合一）與條款。"""
    html = client.get("/privacy").get_data(as_text=True)
    assert 'href="/register"' not in html and "前往註冊" not in html
    assert 'href="/login"' in html and 'href="/terms"' in html


def test_the_terms_say_new_accounts_start_in_the_safe_mode(client):
    """adult-only-signup（design D5）：新帳號的預設內容模式是安全模式；條款不得再說成年者預設一般模式。"""
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    assert "新帳號預設使用安全模式，可以自行調整為一般模式" in section
    assert "預設使用一般模式" not in section and "成年的使用者預設" not in section
    assert "成人模式預設關閉" in section                                            # 成人模式的說明不變（留給 safe-mode-toggle）


# ---- RPG、劇本與主角（rpg-script-authoring、rpg-character-creation）新增的揭露 ----------------------------------

def test_the_retention_table_lists_the_rpg_rows_with_the_same_periods_as_the_purge_command(client):
    table = retention_table(client.get("/privacy").get_data(as_text=True))
    (save_label,) = [key for key in table if key.startswith("RPG 存檔")]
    assert "主角" in save_label and period_text(retention.SESSION_RETENTION) in table[save_label] and "刪除帳號" in table[save_label]
    (submission_label,) = [key for key in table if key.startswith("劇本送審紀錄")]
    assert "只含結果、類別與時間" in submission_label and period_text(retention.SCRIPT_SUBMISSION_RETENTION) in table[submission_label]
    (script_label,) = [key for key in table if key.startswith("劇本（")]
    assert "刪除帳號時一併刪除" in table[script_label]
    (intro_label,) = [key for key in table if "自我介紹" in key]
    assert table[intro_label] == "不保存"
    # 每一列只含自己的期限
    assert "12 個月" not in table["管理稽核紀錄"]


def test_the_privacy_policy_says_what_is_sent_to_the_rpg_model_service_and_that_it_is_separate_from_gemini(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>4. RPG 與劇本的資料會傳送至 RPG 模型服務</h2>", 1)[1].split("<h2>5.", 1)[0]
    for needle in ("OpenRouter", "與客服使用的 Google Gemini 分開", "劇本設定", "主角", "自我介紹", "不含隱藏特質", "免費模型的輸入輸出可能被供應商留存",
                   "勿在劇本、主角與自我介紹中填寫真實的個人資料", "零保存", "信箱、生日與年齡區間", "不會", "只告知類別"):
        assert needle in section, needle


def test_the_privacy_policy_lists_the_new_personal_data_and_says_the_intro_is_never_stored_and_admins_cannot_read_scripts_by_default(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("暱稱與短代號", "筆名", "管理員預設看不到", "稽核紀錄", "只保留<strong>類別與時間</strong>", "存檔與主角", "不會儲存"):
        assert needle in section, needle


def test_the_rights_section_covers_the_exported_and_deleted_rpg_data(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    for needle in ("暱稱與短代號", "您創作的劇本（全部版本）", "存檔（含主角與聯繫）", "送審紀錄", "自我介紹不保存", "以及您的暱稱、劇本、存檔與主角"):
        assert needle in section, needle


def test_the_privacy_policy_does_not_promise_features_that_do_not_exist_yet(client):
    html = client.get("/privacy").get_data(as_text=True)
    assert "目前沒有公開分享" not in html and "現階段沒有公開分享" in html
    for forbidden in ("將提供", "後續版本提供", "將於"):
        assert forbidden not in html


def test_the_terms_describe_the_rpg_the_content_modes_and_the_absolute_prohibitions(client):
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    for needle in ("安全模式、一般模式與成人模式", "成人模式預設關閉", "自行聲明", "無論哪一種模式，一律禁止", "未成年（含看似未成年的角色）的性內容",
                   "性暴力", "非自願的性情節", "以真人名義的性內容", "我們只告知類別", "暫停而不是放行", "暫停送審權限", "申訴頁",
                   "筆名", "先審後發", "授權本平台保存", "不含您的署名", "公開分享尚未開放", "AI 生成的主角與聯繫只是建議", "性奴等禁斷關係", "隨帳號一併刪除"):
        assert needle in section, needle
    assert "RPG" in html.split("<h2>2.", 1)[0] and "劇本創作" in html.split("<h2>2.", 1)[0]          # 服務內容也提到了


def test_the_age_numbers_in_the_new_terms_section_come_from_the_age_policy(client):
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    assert "13 至 17 歲" not in section and "我確定已滿 18 歲" in section        # 數字來自年齡政策：最低年齡 18，沒有未成年者的固定安全模式說明


# ---- 重新生成與編輯最後一輪（rpg-turn-revision）新增的揭露 ----------------------------------------------------

def test_the_privacy_policy_says_the_regeneration_reason_goes_to_the_operators_never_to_the_ai_and_is_kept_with_the_save(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("重新生成時填寫的分類與理由", "會提供營運團隊檢視", "不含您的帳號資料", "不會傳送給 AI", "不會出現在日誌", "請勿在理由中填寫真實的個人資料"):
        assert needle in section, needle
    table = retention_table(html)
    (label,) = [key for key in table if key.startswith("重新生成時填寫的分類與理由")]
    assert "與所屬存檔相同" in table[label] and "存檔刪除或帳號刪除時一併刪除" in table[label]


def test_the_model_service_section_says_the_reason_is_not_sent_to_the_rpg_model(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>4. RPG 與劇本的資料會傳送至 RPG 模型服務</h2>", 1)[1].split("<h2>5.", 1)[0]
    assert "分類與理由不會</strong>傳送至這個服務" in section and "不會成為 AI 的指令" in section


def test_the_privacy_policy_says_the_wallet_and_ledger_stay_with_the_account_and_are_exported_and_deleted(client):
    html = client.get("/privacy").get_data(as_text=True)
    section = html.split("<h2>1. 我們蒐集哪些資料</h2>", 1)[1].split("<h2>2.", 1)[0]
    for needle in ("遊戲幣與帳本", "免費與付費兩種", "每一筆的種類、增減的金額與關聯的存檔", "隨帳號保存"):
        assert needle in section, needle
    table = retention_table(html)
    (label,) = [key for key in table if key.startswith("遊戲幣餘額與帳本")]
    assert "每筆的種類、金額與關聯的存檔" in label and "帳號存續期間" in table[label] and "刪除帳號時一併刪除" in table[label]
    rights = html.split("<h2>6. 您的權利</h2>", 1)[1].split("<h2>7.", 1)[0]
    for needle in ("遊戲幣的餘額與帳本", "重新生成時填寫的分類與理由", "遊戲幣錢包與帳本，無法復原", "含重新生成時填寫的理由"):
        assert needle in rights, needle


def test_the_privacy_policy_still_does_not_promise_payment_features(client):
    html = client.get("/privacy").get_data(as_text=True)
    for forbidden in ("儲值", "訂閱", "付款紀錄", "退款"):
        assert forbidden not in html, forbidden


def test_the_terms_describe_the_regeneration_and_edit_rules(client):
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    for needle in ("重新生成與編輯最後一輪", "最後一輪", "AI 的輸出不能直接修改", "下一輪開始後這一輪就不能再動",
                   # 2026-11-16 規則修訂：兩種操作共用同一組免費次數；免費次數內編輯不扣幣也不需要餘額；編輯不使次數歸零
                   "前幾次重新生成或編輯免費", "兩種操作共用同一組次數", "免費次數內不扣遊戲幣，也不需要有餘額",
                   "每次重新生成或編輯都消耗與一輪劇情相同的遊戲幣", "原本那一輪已扣的幣不退", "餘額不足時無法送出", "編輯不會使免費次數歸零",
                   "必須選擇分類並填寫", "理由會提供營運團隊檢視、不會送給 AI", "完整通過內容檢查", "原本的回應會保留", "不扣遊戲幣，也不消耗免費次數",
                   "遊戲幣帳本會隨帳號一併刪除"):
        assert needle in section, needle
    assert "沒有成功產生回應" in section and "以遊戲內顯示為準" in section


def test_the_terms_no_longer_carry_the_old_edit_always_paid_and_resets_the_free_count_rule(client):
    """舊規則（編輯一律消耗幣並使免費次數歸零）已於 2026-11-16 作廢；條款不得再留下這兩句。"""
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    for stale in ("編輯同樣消耗一輪的遊戲幣", "使該回合的免費重新生成次數歸零", "免費重新生成次數歸零"):
        assert stale not in section, stale
    # 「歸零」只允許出現在「編輯不會使免費次數歸零」這一句
    assert section.count("歸零") == 1 and "編輯不會使免費次數歸零" in section


def test_the_reason_length_range_in_both_pages_comes_from_the_service_constants_not_from_the_text(client, monkeypatch):
    from app.services.game_service import REASON_MAX, REASON_MIN

    for path in ("/privacy", "/terms"):
        assert f"{REASON_MIN} 至 {REASON_MAX} 字" in client.get(path).get_data(as_text=True), path
    monkeypatch.setattr("app.controllers.auth_page_controller.REASON_MIN", 7)
    monkeypatch.setattr("app.controllers.auth_page_controller.REASON_MAX", 63)
    for path in ("/privacy", "/terms"):
        html = client.get(path).get_data(as_text=True)
        assert "7 至 63 字" in html and f"{REASON_MIN} 至 {REASON_MAX} 字" not in html, path


def test_the_new_terms_text_hardcodes_no_coin_or_free_regeneration_numbers(client):
    """幣價與免費次數存在設定表、可用指令調整；條款只講規則，數字以遊戲內顯示為準，所以不會與設定脫節。"""
    html = client.get("/terms").get_data(as_text=True)
    section = html.split("<h2>5. RPG、劇本創作與內容規範</h2>", 1)[1].split("<h2>6.", 1)[0]
    revision = section.split("<strong>遊戲幣</strong>", 1)[1].split("<strong>刪除帳號</strong>", 1)[0]
    assert not re.search(r"\d+\s*(?:幣|枚)|前\s*\d+\s*次|第\s*\d+\s*次", revision)
